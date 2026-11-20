"""Small geometric-vision helpers shared by the analysis tools."""
from __future__ import annotations

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation as R


def kabsch(a: np.ndarray, b: np.ndarray, w: np.ndarray | None = None) -> np.ndarray:
    """rotation matrix M minimising sum w |b - M a|^2."""
    w = np.ones(len(a)) if w is None else w
    H = (a * w[:, None]).T @ b
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    return Vt.T @ np.diag([1, 1, d]) @ U.T


def rel_pose(a: np.ndarray, b: np.ndarray, R0: R | None = None) -> R:
    """rotation of a two-view spherical relative pose (rotation + translation direction).
    a, b: matched unit rays in view i / view j. Robust epipolar fit b.(t x R a) = 0."""
    if R0 is None:
        w = np.ones(len(a))
        for _ in range(4):
            M = kabsch(a, b, w)
            w = 1 / np.maximum(np.linalg.norm(b - a @ M.T, axis=1), 2e-3)
        R0 = R.from_matrix(M)
    flow = b - R0.apply(a)
    t0 = np.linalg.svd(np.cross(R0.apply(a), flow))[2][-1]

    def res(x):
        Rm = R.from_rotvec(x[:3]) * R0
        th, ph = x[3], x[4]
        t = np.array([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)])
        n = np.cross(t, Rm.apply(a))
        return (b * n).sum(1) / np.maximum(np.linalg.norm(n, axis=1), 1e-9)

    th0 = np.arccos(np.clip(t0[2], -1, 1))
    ph0 = np.arctan2(t0[1], t0[0])
    sol = least_squares(res, np.r_[0, 0, 0, th0, ph0], loss="cauchy", f_scale=2e-3)
    return R.from_rotvec(sol.x[:3]) * R0


def equirect_pixel_rays(p: np.ndarray, width: int, height: int) -> np.ndarray:
    lon = ((p[:, 0] + 0.5) / width * 2 - 1) * np.pi
    lat = (0.5 - (p[:, 1] + 0.5) / height) * np.pi
    return np.stack([np.cos(lat) * np.sin(lon), -np.sin(lat), np.cos(lat) * np.cos(lon)], -1)


def track_rotation(prev: np.ndarray, cur: np.ndarray, mask: np.ndarray | None = None,
                   max_pts: int = 800) -> R | None:
    """relative rotation view_cur_from_view_prev between two equirect gray frames."""
    h, w = prev.shape
    p0 = cv2.goodFeaturesToTrack(prev, max_pts, 0.01, 8, mask=mask)
    if p0 is None or len(p0) < 20:
        return None
    p1, st, _ = cv2.calcOpticalFlowPyrLK(prev, cur, p0, None, winSize=(21, 21), maxLevel=3)
    ok = st.ravel() == 1
    if ok.sum() < 20:
        return None
    a = equirect_pixel_rays(p0.reshape(-1, 2)[ok], w, h)
    b = equirect_pixel_rays(p1.reshape(-1, 2)[ok], w, h)
    return rel_pose(a, b)
