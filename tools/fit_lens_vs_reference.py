"""Measure the true fisheye mapping of each lens against a reference equirect.

Matches SIFT features between a raw lens frame and the same frame of a
known-good equirectangular stitch (e.g. a DJI Studio export), then fits
rotation + radial model by robust least squares. Used once to settle the
calibration conventions of the dvtm_oq101 metadata.

usage: fit_lens_vs_reference.py f0.gray f1.gray ref.gray
"""
import sys

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation as R

N = 3840
EW, EH = 7680, 3840


def load(p, w, h):
    return np.fromfile(p, np.uint8).reshape(h, w)


def eq_dir(u, v):
    """equirect pixel -> unit ray (x right, y down, z forward); lon 0 at centre."""
    lon = (u + 0.5) / EW * 2 * np.pi - np.pi
    lat = np.pi / 2 - (v + 0.5) / EH * np.pi
    return np.stack([np.cos(lat) * np.sin(lon), -np.sin(lat), np.cos(lat) * np.cos(lon)], -1)


def dir_eq(d):
    lon = np.arctan2(d[..., 0], d[..., 2])
    lat = np.arcsin(np.clip(-d[..., 1], -1, 1))
    return (lon + np.pi) / (2 * np.pi) * EW - 0.5, (np.pi / 2 - lat) / np.pi * EH - 0.5


def poly(th, k):
    t2 = th * th
    return th * (1 + t2 * (k[0] + t2 * (k[1] + t2 * (k[2] + t2 * k[3]))))


def project(d, rot, f, cx, cy, k):
    """world ray -> fisheye pixel; rot maps lens->world."""
    dl = rot.inv().apply(d)
    rxy = np.hypot(dl[:, 0], dl[:, 1])
    th = np.arctan2(rxy, dl[:, 2])
    r = f * poly(th, k)
    s = np.where(rxy > 1e-12, r / np.maximum(rxy, 1e-12), 0)
    return np.stack([cx + s * dl[:, 0], cy + s * dl[:, 1]], -1), th


def warp_ref_into_lens(E, rot, f, cx, cy):
    """render reference equirect into the lens' image geometry (equidistant guess)."""
    yy, xx = np.mgrid[0:N, 0:N].astype(np.float32)
    x, y = xx - cx, yy - cy
    r = np.hypot(x, y)
    th = r / f
    s = np.sin(th) / np.maximum(r, 1e-6)
    d = np.stack([x * s, y * s, np.cos(th)], -1).reshape(-1, 3)
    d = rot.apply(d)
    mu, mv = dir_eq(d)
    out = cv2.remap(E, mu.reshape(N, N).astype(np.float32), mv.reshape(N, N).astype(np.float32),
                    cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
    out[th > np.radians(105)] = 0
    return out, d.reshape(N, N, 3)


def fit(F, E, rot0, f0=1047.0, c0=(1920.0, 1920.0)):
    W, dirs = warp_ref_into_lens(E, rot0, f0, *c0)
    sift = cv2.SIFT_create(nfeatures=20000)
    mask = np.zeros_like(F)
    cv2.circle(mask, (N // 2, N // 2), 1880, 255, -1)
    k1, d1 = sift.detectAndCompute(F, mask)
    k2, d2 = sift.detectAndCompute(W, mask)
    m = cv2.BFMatcher().knnMatch(d1, d2, k=2)
    good = [a for a, b in m if a.distance < 0.7 * b.distance]
    p = np.array([k1[g.queryIdx].pt for g in good])
    q = np.array([k2[g.trainIdx].pt for g in good])
    # ray of q: bilinear-free nearest lookup is fine at this density
    rays = dirs[np.clip(q[:, 1].round().astype(int), 0, N - 1), np.clip(q[:, 0].round().astype(int), 0, N - 1)]
    # keep only geometrically consistent matches (displacement small after guess warp)
    disp = np.linalg.norm(p - q, axis=1)
    keep = disp < 80
    p, rays = p[keep], rays[keep]
    print(f"  matches: {len(good)} -> {len(p)} consistent")

    def resid(x):
        rot = R.from_rotvec(x[:3]) * rot0
        uv, _ = project(rays, rot, x[3], x[4], x[5], x[6:10])
        return (uv - p).ravel()

    x0 = np.r_[0, 0, 0, f0, c0[0], c0[1], 0, 0, 0, 0]
    sol = least_squares(resid, x0, loss="soft_l1", f_scale=2.0)
    rot = R.from_rotvec(sol.x[:3]) * rot0
    uv, th = project(rays, rot, *sol.x[3:6], sol.x[6:10])
    err = np.linalg.norm(uv - p, axis=1)
    print(f"  median reproj err {np.median(err):.2f}px  90%: {np.percentile(err, 90):.2f}px  "
          f"max theta in data {np.degrees(th.max()):.1f} deg")
    print(f"  f={sol.x[3]:.2f} cx={sol.x[4]:.2f} cy={sol.x[5]:.2f} k={np.round(sol.x[6:10], 6).tolist()}")
    print(f"  rot (lens->ref) quat xyzw={np.round(rot.as_quat(), 5).tolist()}")
    # radial curve samples
    for deg in (30, 60, 80, 90, 95, 100, 105):
        print(f"    r({deg})={sol.x[3] * poly(np.radians(deg), sol.x[6:10]):.1f}px")
    return rot, sol.x, p, rays, th


if __name__ == "__main__":
    F0 = load(sys.argv[1], N, N)
    F1 = load(sys.argv[2], N, N)
    E = load(sys.argv[3], EW, EH)
    # guesses: lens1 (track 2) faces the reference centre, lens0 faces backwards.
    print("lens1 (track 2)")
    r1, x1, *_ = fit(F1, E, R.identity())
    print("lens0 (track 1)")
    r0, x0, *_ = fit(F0, E, R.from_euler("y", 180, degrees=True))
    rel = r1.inv() * r0
    print("relative lens0 in lens1 frame, quat xyzw:", np.round(rel.as_quat(), 5).tolist(),
          "angle", np.degrees(rel.magnitude()).round(3))
