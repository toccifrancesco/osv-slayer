"""wgpu (Vulkan) host side of the stitch shader."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import wgpu
from scipy.spatial.transform import Rotation as R

from ..lens import Rig

SHADER = (Path(__file__).parent / "stitch.wgsl").read_text()
LENS_FLOATS = 32  # 8 x vec4
PARAM_FLOATS = 2 * LENS_FLOATS + 4


@dataclass
class LensFrameParams:
    """per-frame, per-lens dynamic parameters."""
    gain_y: float = 1.0
    gain_u: float = 1.0
    gain_v: float = 1.0
    off_u: float = 0.0
    off_v: float = 0.0
    omega_lens: tuple = (0.0, 0.0, 0.0)  # rad/s in the lens frame


class GPUStitcher:
    def __init__(self, rig: Rig, out_w: int, out_h: int, in_size: int = 3840,
                 readout_s: float = 0.0, black_level: float = 64 / 1023.984375):
        assert out_w % 2 == 0 and out_h % 2 == 0
        self.rig, self.W, self.H, self.N = rig, out_w, out_h, in_size
        self.readout_s = readout_s
        self.black = black_level
        adapter = wgpu.gpu.request_adapter_sync(power_preference="high-performance")
        self.adapter_info = adapter.info
        self.dev = adapter.request_device_sync(
            required_features=["texture-format16bit-norm"],
            required_limits={"max-storage-buffer-binding-size": adapter.limits["max-storage-buffer-binding-size"],
                             "max-buffer-size": adapter.limits["max-buffer-size"]})
        d = self.dev
        TU = wgpu.TextureUsage
        self.ytex = [d.create_texture(size=(in_size, in_size, 1), format="r16unorm",
                                      usage=TU.TEXTURE_BINDING | TU.COPY_DST) for _ in range(2)]
        self.ctex = [d.create_texture(size=(in_size // 2, in_size // 2, 1), format="rg16unorm",
                                      usage=TU.TEXTURE_BINDING | TU.COPY_DST) for _ in range(2)]
        BU = wgpu.BufferUsage
        self.ybytes = out_w * out_h * 2
        self.cbytes = out_w * out_h  # (W/2 * H/2) * 2 ch * 2 bytes
        self.oy = d.create_buffer(size=self.ybytes, usage=BU.STORAGE | BU.COPY_SRC)
        self.oc = d.create_buffer(size=self.cbytes, usage=BU.STORAGE | BU.COPY_SRC)
        self.read = d.create_buffer(size=self.ybytes + self.cbytes, usage=BU.MAP_READ | BU.COPY_DST)
        self.ubuf = d.create_buffer(size=PARAM_FLOATS * 4, usage=BU.UNIFORM | BU.COPY_DST)
        samp = d.create_sampler(mag_filter="linear", min_filter="linear",
                                address_mode_u="clamp-to-edge", address_mode_v="clamp-to-edge")
        module = d.create_shader_module(code=SHADER)
        self.pipe = d.create_compute_pipeline(layout="auto", compute={"module": module, "entry_point": "main"})
        entries = [
            {"binding": 0, "resource": {"buffer": self.ubuf}},
            {"binding": 1, "resource": samp},
            {"binding": 2, "resource": self.ytex[0].create_view()},
            {"binding": 3, "resource": self.ctex[0].create_view()},
            {"binding": 4, "resource": self.ytex[1].create_view()},
            {"binding": 5, "resource": self.ctex[1].create_view()},
            {"binding": 6, "resource": {"buffer": self.oy}},
            {"binding": 7, "resource": {"buffer": self.oc}},
        ]
        self.bind = d.create_bind_group(layout=self.pipe.get_bind_group_layout(0), entries=entries)

    # ------------------------------------------------------------------ params
    def _params(self, body_from_view: R, lp: list[LensFrameParams]) -> np.ndarray:
        p = np.zeros(PARAM_FLOATS, np.float32)
        for i, lens in enumerate(self.rig.lenses):
            m = (lens.rot.inv() * body_from_view).as_matrix()
            o = i * LENS_FLOATS
            p[o + 0:o + 3], p[o + 4:o + 7], p[o + 8:o + 11] = m[0], m[1], m[2]
            p[o + 12:o + 16] = [lens.fx, lens.fy, lens.cx, lens.cy]
            p[o + 16:o + 20] = lens.k
            p[o + 20:o + 23] = lp[i].omega_lens
            p[o + 23] = self.readout_s
            p[o + 24:o + 28] = [lp[i].gain_y, lp[i].gain_u, lp[i].gain_v, self.black]
            p[o + 28:o + 32] = [np.radians(lens.max_theta_deg), lens.width, lp[i].off_u, lp[i].off_v]
        p[2 * LENS_FLOATS:] = [self.W, self.H, np.radians(self.rig.blend_deg), 0]
        return p

    # ------------------------------------------------------------------ frame
    def upload(self, i: int, p010: np.ndarray) -> None:
        """p010: uint16 array of N*N*3/2 samples (Y plane then interleaved UV plane)."""
        N = self.N
        q = self.dev.queue
        y = p010[:N * N]
        c = p010[N * N:]
        q.write_texture({"texture": self.ytex[i]}, y, {"bytes_per_row": N * 2, "rows_per_image": N}, (N, N, 1))
        q.write_texture({"texture": self.ctex[i]}, c, {"bytes_per_row": N * 2, "rows_per_image": N // 2},
                        (N // 2, N // 2, 1))

    def render(self, body_from_view: R, lens_params: list[LensFrameParams] | None = None,
               out: np.ndarray | None = None) -> np.ndarray:
        """run the stitch on the uploaded frames; returns P010 (uint16, W*H*3/2).
        `out`: optional preallocated destination (avoids page-faulting a fresh 88 MB array)."""
        lp = lens_params or [LensFrameParams(), LensFrameParams()]
        d = self.dev
        d.queue.write_buffer(self.ubuf, 0, self._params(body_from_view, lp))
        enc = d.create_command_encoder()
        cp = enc.begin_compute_pass()
        cp.set_pipeline(self.pipe)
        cp.set_bind_group(0, self.bind)
        cp.dispatch_workgroups((self.W // 2 + 15) // 16, (self.H // 2 + 7) // 8, 1)
        cp.end()
        enc.copy_buffer_to_buffer(self.oy, 0, self.read, 0, self.ybytes)
        enc.copy_buffer_to_buffer(self.oc, 0, self.read, self.ybytes, self.cbytes)
        d.queue.submit([enc.finish()])
        self.read.map_sync(wgpu.MapMode.READ)
        view = np.frombuffer(self.read.read_mapped(copy=False), np.uint16)
        if out is None:
            out = view.copy()
        else:
            np.copyto(out, view)
        del view
        self.read.unmap()
        return out
