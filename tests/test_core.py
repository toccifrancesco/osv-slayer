import subprocess
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial.transform import Rotation as R

from osv360 import spherical
from osv360.cpu_ref import stitch
from osv360.lens import Lens, Rig, equirect_rays, rays_to_equirect
from osv360.motion import VIEW_FROM_WORLD0, StabParams, view_rotations

ROOT = Path(__file__).resolve().parents[1]
OSV = next(iter(sorted((ROOT / "footage").glob("*.OSV"))), None)
needs_osv = pytest.mark.skipif(OSV is None, reason="no .OSV footage available")
needs_ffmpeg = pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg not installed")

FACTORY = [
    Lens(1047.02, 1046.66, 1903.80, 1921.07, [0.0656365, -0.0109562, 0.00865739, -0.00617398],
         [0.002289, 0.000315, -0.70323, 0.710959]),
    Lens(1049.22, 1049.24, 1911.33, 1910.99, [0.0634959, -0.00894781, 0.00747729, -0.00575172],
         [0.702883, 0.711304, 0.001289, -0.00057]),
]


def rig():
    return Rig([l.extended() for l in FACTORY])


# ------------------------------------------------------------------ container + metadata
@needs_osv
def test_mp4_tracks():
    from osv360.mp4 import MP4
    with MP4(OSV) as m:
        codecs = [t.codec for t in m.tracks]
        assert codecs.count("hvc1") == 2 and codecs.count("djmd") == 2
        v = m.tracks_by_codec("hvc1")[0]
        d = m.tracks_by_codec("djmd")[0]
        assert v.sample_count == d.sample_count
        assert all(o > 0 for o in d.sample_offsets[:10])


@needs_osv
def test_djmd_imu_rate():
    from osv360.djmd import read_meta
    meta = read_meta(OSV)
    assert meta.proto == "dvtm_oq101.proto"
    assert meta.imu_rate == 1000
    span_imu = (meta.imu_ts_us[-1] - meta.imu_ts_us[0]) / 1e6
    span_vid = (meta.frame_ts_us[-1] - meta.frame_ts_us[0]) / 1e6
    # the old decoder assumed 800 Hz which stretched the IMU by 25%; it must match the video
    assert abs(span_imu - span_vid) < 0.1
    assert 19.5 < len(meta.imu_ts_us) / len(meta.frame_ts_us) < 20.5
    assert np.allclose(np.linalg.norm(meta.imu_quat_wxyz, axis=1), 1, atol=1e-5)


# ------------------------------------------------------------------ lens model
def test_extended_curve_is_monotone_past_overlap():
    for l in rig().lenses:
        th, _r = l.radial_table()
        assert np.degrees(th[-1]) > 105  # covers the overlap zone
    # inner field unchanged vs the factory polynomial
    for fac, ext in zip(FACTORY, rig().lenses):
        t = np.radians(np.linspace(1, 55, 30))
        assert np.max(np.abs(fac.theta_to_r(t) - ext.theta_to_r(t)) * fac.fx) < 0.5


def test_project_unproject_roundtrip():
    l = rig().lenses[0]
    rng = np.random.default_rng(1)
    th = np.radians(rng.uniform(0, 100, 5000))
    ph = rng.uniform(-np.pi, np.pi, 5000)
    d = np.stack([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)], -1)
    uv, _ = l.project(d)
    d2 = l.unproject(uv)
    uv2, _ = l.project(d2)
    assert np.max(np.linalg.norm(uv - uv2, axis=1)) < 0.01


def test_equirect_ray_roundtrip():
    d = equirect_rays(64, 32).reshape(-1, 3)
    u, v = rays_to_equirect(d, 64, 32)
    uu, vv = np.meshgrid(np.arange(64), np.arange(32))
    assert np.allclose(u, uu.ravel(), atol=1e-6) and np.allclose(v, vv.ravel(), atol=1e-6)


# ------------------------------------------------------------------ motion conventions
def test_horizon_mode_levels_the_view():
    rng = np.random.default_rng(0)
    world_from_body = R.from_rotvec(rng.normal(0, 0.4, (200, 3)))  # wild camera motion
    rg = rig()
    fwd = rg.view_from_body.inv().apply([0, 0, 1])
    body_from_view = view_rotations(world_from_body, 50, fwd, StabParams("horizon"))
    world_from_view = world_from_body * body_from_view
    down_view = world_from_view.apply([0, 1, 0])  # view +y (down) expressed in world
    assert np.allclose(down_view, [0, 0, 1], atol=1e-9)  # world is +z down (NED)


def test_lock_mode_fixed_heading():
    rng = np.random.default_rng(0)
    world_from_body = R.from_rotvec(rng.normal(0, 0.3, (100, 3)))
    fwd = rig().view_from_body.inv().apply([0, 0, 1])
    bv = view_rotations(world_from_body, 50, fwd, StabParams("lock"))
    f = (world_from_body * bv).apply([0, 0, 1])
    assert np.allclose(f, f[0], atol=1e-9)


def test_view_from_world0_is_proper_rotation():
    assert np.isclose(np.linalg.det(VIEW_FROM_WORLD0.as_matrix()), 1)


# ------------------------------------------------------------------ GPU == CPU reference
def _gpu_available():
    try:
        import wgpu
        return wgpu.gpu.request_adapter_sync() is not None
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _gpu_available(), reason="no wgpu adapter")
def test_gpu_matches_cpu_reference():
    from osv360.gpu.stitcher import GPUStitcher
    N = 512
    rg = rig().scaled(N)
    rng = np.random.default_rng(3)
    import cv2
    frames = []
    for _ in range(2):
        y = cv2.GaussianBlur(rng.uniform(64, 940, (N, N)).astype(np.float32), (0, 0), 3)
        c = np.full((N // 2, N // 2, 2), 512.0, np.float32)
        frames.append((y, np.concatenate([(y.astype(np.uint16) << 6).ravel(),
                                          (c.astype(np.uint16) << 6).ravel()])))
    g = GPUStitcher(rg, 512, 256, N)
    for i in (0, 1):
        g.upload(i, frames[i][1])
    bv = R.from_euler("xyz", [5, -10, 20], degrees=True) * rg.view_from_body.inv()
    out = g.render(bv)
    Y = (out[:512 * 256].reshape(256, 512) >> 6).astype(np.float32)
    ref = stitch([frames[0][0], frames[1][0]], rg, 512, 256, body_from_view=bv)
    d = np.abs(Y - ref)
    assert np.median(d) < 0.6 and np.percentile(d, 99) < 3


# ------------------------------------------------------------------ spherical metadata
@needs_ffmpeg
def test_spherical_injection(tmp_path):
    out = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=128x64:r=10", "-frames:v",
                    "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
    spherical.inject(out)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", str(out)], capture_output=True,
                           text=True, check=True).stdout
    assert "projection=equirectangular" in probe
    # still decodes
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"], check=True)


# ------------------------------------------------------------------ CLI / GUI plumbing
def test_output_path_template():
    from osv360.cli import output_path
    assert output_path("/x/CAM_01.OSV", "/out", "{name}_{mode}", "horizon", "hevc") == Path("/out/CAM_01_horizon.mp4")
    assert output_path("a/b.OSV", "o", "{name}-{codec}", "lock", "prores") == Path("o/b-prores.mov")


def test_cli_parser_defaults():
    from osv360.cli import build_options, build_parser
    a = build_parser().parse_args(["convert", "x.OSV"])
    o = build_options(a)
    assert (o.width, o.height) == (7680, 3840)
    assert o.stab.mode == "horizon" and o.encode.qp == 27 and o.encode.codec == "hevc"


def test_gui_settings_to_cli_args():
    pytest.importorskip("PySide6")
    from osv360.cli import build_options, build_parser
    from osv360.gui.settings import AppSettings
    s = AppSettings(stabilize="lock", qp=30, scale=0.5, exposure_match=False, overwrite=True)
    a = build_parser().parse_args(["convert", "x.OSV", *s.cli_args()])
    o = build_options(a)
    assert o.stab.mode == "lock" and o.encode.qp == 30 and o.width == 3840 and not o.exposure_match
    assert a.overwrite


def test_selftest_command():
    from osv360.cli import main
    assert main(["selftest"]) == 0
