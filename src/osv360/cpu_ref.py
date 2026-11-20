"""NumPy/OpenCV reference implementation of the stitch.

Same math as gpu/stitch.wgsl. Used for stills, calibration refinement and as
the test oracle for the GPU path.
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.spatial.transform import Rotation as R

from .lens import Rig, equirect_rays


def lens_weight(theta0: np.ndarray, theta1: np.ndarray, rig: Rig) -> np.ndarray:
    """blend weight of lens 0 (lens 1 gets 1-w). Seam where both lenses see the ray
    at the same angle from their axis; linear feather of `blend_deg` across it."""
    b = np.radians(max(rig.blend_deg, 1e-3))
    w = np.clip((theta1 - theta0) / b + 0.5, 0.0, 1.0)
    w = w * w * (3 - 2 * w)  # smoothstep
    m0 = np.radians(rig.lenses[0].max_theta_deg)
    m1 = np.radians(rig.lenses[1].max_theta_deg)
    w = np.where(theta0 > m0, 0.0, w)
    w = np.where(theta1 > m1, 1.0, w)
    return w


def stitch_maps(rig: Rig, width: int, height: int, body_from_view: R | None = None):
    """per-lens remap tables (map_x, map_y as float32) and lens-0 weight (float32)."""
    d = equirect_rays(width, height).reshape(-1, 3)
    bv = rig.view_from_body.inv() if body_from_view is None else body_from_view
    d_body = bv.apply(d)
    maps, thetas = [], []
    for lens in rig.lenses:
        dl = lens.rot.inv().apply(d_body)
        uv, th = lens.project(dl)
        maps.append((uv[:, 0].reshape(height, width).astype(np.float32),
                     uv[:, 1].reshape(height, width).astype(np.float32)))
        thetas.append(th.reshape(height, width))
    w0 = lens_weight(thetas[0], thetas[1], rig).astype(np.float32)
    return maps, w0, thetas


def remap(img: np.ndarray, mx: np.ndarray, my: np.ndarray) -> np.ndarray:
    return cv2.remap(img, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)


def stitch(frames, rig: Rig, width: int, height: int, body_from_view: R | None = None,
           gains=(1.0, 1.0)) -> np.ndarray:
    """frames: [lens0, lens1] images in the lens' native resolution. Returns float32 equirect."""
    maps, w0, _ = stitch_maps(rig, width, height, body_from_view)
    a = remap(frames[0].astype(np.float32) * gains[0], *maps[0])
    b = remap(frames[1].astype(np.float32) * gains[1], *maps[1])
    if a.ndim == 3:
        w0 = w0[..., None]
    return a * w0 + b * (1 - w0)


def seam_flow(frames, rig: Rig, width: int = 2048, height: int = 1024, band_deg: float = 8.0):
    """Measure misalignment between the two lenses inside the overlap band.

    Each lens is rendered on its own into the equirect; dense DIS optical flow
    between the two renders over pixels where both lenses are valid and within
    `band_deg` of the seam. Returns (median |flow| in px at this resolution,
    flow field, mask, render0, render1)."""
    maps, _w0, (t0, t1) = stitch_maps(rig, width, height)
    g = [f if f.ndim == 2 else cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    a = remap(g[0], *maps[0])
    b = remap(g[1], *maps[1])
    lim0 = np.radians(rig.lenses[0].max_theta_deg)
    lim1 = np.radians(rig.lenses[1].max_theta_deg)
    mask = (np.abs(t0 - t1) < np.radians(band_deg)) & (t0 < lim0) & (t1 < lim1)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    a8 = np.clip(a, 0, 255).astype(np.uint8)
    b8 = np.clip(b, 0, 255).astype(np.uint8)
    flow = dis.calc(a8, b8, None)
    # ignore textureless pixels: flow is meaningless there
    gx = cv2.Sobel(a8, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(a8, cv2.CV_32F, 0, 1)
    tex = np.hypot(gx, gy) > 40
    m = mask & tex
    mag = np.linalg.norm(flow, axis=-1)
    med = float(np.median(mag[m])) if m.any() else float("nan")
    return med, flow, m, a, b
