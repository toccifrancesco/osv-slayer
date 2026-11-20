"""Fisheye lens model and rig geometry.

Conventions (used everywhere, CPU and GPU):
  * camera / lens coordinates: x right, y down, z along the optical axis.
  * radial model (Kannala-Brandt):  r = f * theta * (1 + k1 t^2 + k2 t^4 + k3 t^6 + k4 t^8)
    with theta the angle from the optical axis; pixel = (cx, cy) + r * (x, y)/|(x, y)|.
  * body frame: the frame the dvtm_oq101 lens quaternions (xyzw, lens->body) live in.
    lens 0 looks along body +z, lens 1 along body -z, image "down" is body +x.
  * equirect / view frame: x right, y down, z forward, longitude 0 at the image centre.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation as R


@dataclass
class Lens:
    fx: float
    fy: float
    cx: float
    cy: float
    k: list[float]  # k1..k4
    q_body: list[float]  # xyzw, lens -> body
    width: int = 3840
    height: int = 3840
    max_theta_deg: float = 104.0  # usable field (half-angle)

    @property
    def rot(self) -> R:
        return R.from_quat(self.q_body)

    # -------------------------------------------------------------- radial
    def theta_to_r(self, th):
        t2 = th * th
        k1, k2, k3, k4 = self.k
        return th * (1 + t2 * (k1 + t2 * (k2 + t2 * (k3 + t2 * k4))))

    def project(self, d_lens: np.ndarray):
        """lens-frame rays (...,3) -> pixel coords (...,2), theta."""
        x, y, z = d_lens[..., 0], d_lens[..., 1], d_lens[..., 2]
        rxy = np.hypot(x, y)
        th = np.arctan2(rxy, z)
        rd = self.theta_to_r(th)
        s = np.where(rxy > 1e-12, rd / np.maximum(rxy, 1e-12), 0.0)
        return np.stack([self.cx + self.fx * s * x, self.cy + self.fy * s * y], -1), th

    def radial_table(self, max_deg: float = 120.0, n: int = 4096):
        """monotone (theta, r_norm) table: the polynomial is truncated where it stops increasing."""
        th = np.linspace(0, np.radians(max_deg), n)
        r = self.theta_to_r(th)
        dr = np.diff(r)
        stop = np.argmax(dr <= 0) if (dr <= 0).any() else len(dr)
        return th[: stop + 1], r[: stop + 1]

    def extended(self, trust_deg: float = 60.0, max_deg: float = 110.0) -> "Lens":
        """copy whose radial polynomial matches this one up to `trust_deg` and continues
        monotonically (linear extrapolation of the slope) up to `max_deg`.

        The factory polynomial of the Osmo 360 peaks at ~90 deg and cannot describe
        the overlap zone; this gives a sane starting point for calibrate.refine()."""
        from scipy.optimize import least_squares

        ti = np.radians(np.linspace(1, trust_deg, 60))
        to = np.radians(np.linspace(trust_deg, max_deg, 40))
        t0 = np.radians(trust_deg)
        r0 = self.theta_to_r(t0)
        slope = (self.theta_to_r(t0 + 1e-4) - self.theta_to_r(t0 - 1e-4)) / 2e-4
        target_out = r0 + slope * (to - t0)
        probe = Lens(self.fx, self.fy, self.cx, self.cy, list(self.k), list(self.q_body))

        def res(k):
            probe.k = list(k)
            return np.r_[(probe.theta_to_r(ti) - self.theta_to_r(ti)) * 10,
                         probe.theta_to_r(to) - target_out]

        k = least_squares(res, np.array(self.k)).x
        return Lens(self.fx, self.fy, self.cx, self.cy, [float(x) for x in k], list(self.q_body),
                    self.width, self.height, self.max_theta_deg)

    def unproject(self, uv: np.ndarray):
        """pixel coords (...,2) -> unit lens-frame rays (table inversion of the radial curve;
        radii beyond the monotone range clamp to its end)."""
        mx = (uv[..., 0] - self.cx) / self.fx
        my = (uv[..., 1] - self.cy) / self.fy
        rd = np.hypot(mx, my)
        tt, rr = self.radial_table()
        th = np.interp(rd, rr, tt)
        s = np.where(rd > 1e-12, np.sin(th) / np.maximum(rd, 1e-12), 0.0)
        return np.stack([mx * s, my * s, np.cos(th)], -1)


@dataclass
class Rig:
    lenses: list[Lens]
    # body -> view frame for the "unstabilized" output (forward = lens 1, as DJI Studio does)
    q_view_from_body: list[float] = field(
        default_factory=lambda: R.from_matrix([[0, 1, 0], [1, 0, 0], [0, 0, -1]]).as_quat().tolist())
    # per-lens track index inside the .OSV (video stream index)
    tracks: list[int] = field(default_factory=lambda: [0, 1])
    blend_deg: float = 6.0  # feather width around the seam
    # IMU mounting (body_from_imu, xyzw) and IMU-vs-frame-timestamp offset (s), see tools/imu_align.py
    # (Osmo 360, 3.OSV: hand-eye residual 0.020 deg per 0.1 s step; 0 ms offset would leave 0.11 deg)
    q_body_from_imu: list[float] = field(default_factory=lambda: [-0.006502, 0.706029, 0.003772, 0.708143])
    imu_offset_s: float = 0.007

    @property
    def view_from_body(self) -> R:
        return R.from_quat(self.q_view_from_body)

    def scaled(self, size: int) -> "Rig":
        """same rig for lens images resampled to size x size."""
        import copy
        rig = copy.deepcopy(self)
        for l in rig.lenses:
            s = size / l.width
            l.fx, l.fy = l.fx * s, l.fy * s
            l.cx, l.cy = (l.cx + 0.5) * s - 0.5, (l.cy + 0.5) * s - 0.5
            l.width = l.height = size
        return rig

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "Rig":
        d = json.loads(Path(path).read_text())
        d["lenses"] = [Lens(**l) for l in d["lenses"]]
        return cls(**d)


# ---------------------------------------------------------------- equirect helpers
def equirect_rays(width: int, height: int, dtype=np.float64) -> np.ndarray:
    """unit rays (H,W,3) for pixel centres of an equirect image (view frame)."""
    lon = ((np.arange(width, dtype=dtype) + 0.5) / width * 2 - 1) * np.pi
    lat = (0.5 - (np.arange(height, dtype=dtype) + 0.5) / height) * np.pi
    lon, lat = np.meshgrid(lon, lat)
    cl = np.cos(lat)
    return np.stack([cl * np.sin(lon), -np.sin(lat), cl * np.cos(lon)], -1)


def rays_to_equirect(d: np.ndarray, width: int, height: int):
    lon = np.arctan2(d[..., 0], d[..., 2])
    lat = np.arcsin(np.clip(-d[..., 1], -1, 1))
    return (lon / np.pi + 1) / 2 * width - 0.5, (0.5 - lat / np.pi) * height - 0.5
