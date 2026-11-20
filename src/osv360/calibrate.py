"""Per-clip rig self-calibration from the lens overlap.

The factory polynomial in the metadata is only accurate up to ~60-70 deg off
axis, but the seam lives at ~90 deg. We therefore refine, per clip:
  * each lens' radial curve (f, cx, cy, k1..k4),
  * the rotation of lens 0 relative to lens 1,
using dense optical-flow correspondences between the two lenses inside the
overlap band, sampled from several frames. The factory curve below
`prior_max_deg` and the factory principal point are kept as soft priors, so
the absolute geometry (straight lines, horizon) stays anchored to the
factory calibration while the seam region gets fitted to the footage.
"""
from __future__ import annotations

import copy

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation as R

from .cpu_ref import stitch_maps
from .lens import Rig


def _bilinear(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return cv2.remap(img, x.astype(np.float32).reshape(-1, 1), y.astype(np.float32).reshape(-1, 1),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE).ravel()


def correspondences(frames, rig: Rig, width: int, height: int, band_deg: float,
                    max_shift: float = 0.06, min_lat_deg: float = -50.0, ratio: float = 0.75):
    """fisheye pixel pairs (p0 in lens 0, p1 in lens 1) seeing the same scene point.

    Each lens is rendered alone into an equirect; SIFT features are matched inside
    the overlap band (robust to large initial misalignment), then mapped back to
    the native fisheye pixel coordinates through the remap tables."""
    maps, _w, (t0, t1) = stitch_maps(rig, width, height)
    lim = [np.radians(l.radial_table()[0][-1] * 180 / np.pi - 0.5) for l in rig.lenses]
    lim = [min(lim[i], np.radians(rig.lenses[i].max_theta_deg)) for i in range(2)]
    lat = (0.5 - (np.arange(height) + 0.5) / height) * 180
    band = (np.abs(t0 - t1) < np.radians(band_deg)) & (t0 < lim[0]) & (t1 < lim[1])
    band &= (lat >= min_lat_deg)[:, None]
    mask = (band * 255).astype(np.uint8)
    imgs = [cv2.remap(frames[i], *maps[i], cv2.INTER_LINEAR) for i in range(2)]
    sift = cv2.SIFT_create(nfeatures=6000, contrastThreshold=0.02)
    k0, d0 = sift.detectAndCompute(imgs[0], mask)
    k1, d1 = sift.detectAndCompute(imgs[1], mask)
    if d0 is None or d1 is None or len(k0) < 10 or len(k1) < 10:
        return np.zeros((0, 2)), np.zeros((0, 2)), float("nan")
    q0 = np.array([k.pt for k in k0])
    q1 = np.array([k.pt for k in k1])
    pairs = []
    for a, b in ((m[0], m[1]) for m in cv2.BFMatcher().knnMatch(d0, d1, k=2) if len(m) == 2):
        if a.distance >= ratio * b.distance:
            continue
        dx = q1[a.trainIdx] - q0[a.queryIdx]
        dx[0] = (dx[0] + width / 2) % width - width / 2  # longitude wrap
        if np.hypot(*dx) < max_shift * width:
            pairs.append((a.queryIdx, a.trainIdx))
    if not pairs:
        return np.zeros((0, 2)), np.zeros((0, 2)), float("nan")
    i0, i1 = np.array(pairs).T
    e0, e1 = q0[i0], q1[i1]
    shift = np.hypot(*(e1 - e0).T)

    def lookup(m, e):
        return np.stack([_bilinear(m[0], e[:, 0], e[:, 1]), _bilinear(m[1], e[:, 0], e[:, 1])], -1)

    return lookup(maps[0], e0), lookup(maps[1], e1), float(np.median(shift) * 2048 / width)


def _pack(rig: Rig) -> np.ndarray:
    v = []
    for l in rig.lenses:
        v += [l.fx, l.cx, l.cy, *l.k]
    return np.array(v + [0.0, 0.0, 0.0])


def _unpack(x: np.ndarray, base: Rig) -> Rig:
    rig = copy.deepcopy(base)
    for i, l in enumerate(rig.lenses):
        s = x[7 * i:7 * i + 7]
        aspect = base.lenses[i].fy / base.lenses[i].fx
        l.fx, l.fy, l.cx, l.cy, l.k = s[0], s[0] * aspect, s[1], s[2], list(s[3:7])
    rv = x[14:17]
    rig.lenses[0].q_body = (R.from_rotvec(rv) * base.lenses[0].rot).as_quat().tolist()
    return rig


def _monotone_deg(rig: Rig) -> float:
    return min(np.degrees(l.radial_table()[0][-1]) for l in rig.lenses)


def refine(frames_list, rig: Rig, iterations: int = 4, prior_max_deg: float = 60.0,
           prior_px: float = 0.5, center_sigma_px: float = 5.0, min_monotone_deg: float = 108.0,
           verbose: bool = True) -> Rig:
    """frames_list: list of (lens0_gray, lens1_gray) uint8 full-res pairs.

    Priors (factory curve below `prior_max_deg`, factory principal point) are scaled with
    the number of correspondences so they keep their strength however many matches a clip
    yields: the overlap alone cannot tell a principal-point shift from a lens tilt."""
    factory = copy.deepcopy(rig)
    th_prior = np.radians(np.linspace(5, prior_max_deg, 24))
    r_prior = [l.theta_to_r(th_prior) for l in factory.lenses]
    th_mono = np.radians(np.linspace(prior_max_deg, min_monotone_deg, 40))
    schedule = [(4096, 2048, 14.0), (4096, 2048, 10.0)] + [(4096, 2048, 8.0)] * max(0, iterations - 2)
    cur = rig
    prev_pairs, prev_rig = None, rig
    for it, (w, h, band) in enumerate(schedule[:iterations]):
        P0, P1, meds = [], [], []
        for fr in frames_list:
            p0, p1, med = correspondences(fr, cur, w, h, band)
            P0.append(p0)
            P1.append(p1)
            meds.append(med)
        P0, P1 = np.concatenate(P0), np.concatenate(P1)
        if verbose:
            print(f"  iter {it}: {w}x{h} seam offset median {np.nanmedian(meds):.2f}px@2048, {len(P0)} pairs")
        if len(P0) < 50 or (prev_pairs and len(P0) < 0.3 * prev_pairs):
            if verbose:
                print("  too few correspondences: keeping the previous calibration")
            cur = prev_rig
            break
        prev_pairs, prev_rig = len(P0), cur
        n_scale = np.sqrt(len(P0) / 24.0)  # priors ~ as heavy as the data

        def resid(x):
            rg = _unpack(x, factory)
            d0 = rg.lenses[0].rot.apply(rg.lenses[0].unproject(P0))
            d1 = rg.lenses[1].rot.apply(rg.lenses[1].unproject(P1))
            # angular disagreement, expressed in px of the ~1047 px/rad lens scale
            ang = np.arccos(np.clip((d0 * d1).sum(1), -1, 1)) * 1047.0
            pri = []
            for i, l in enumerate(rg.lenses):
                fac = factory.lenses[i]
                pri.append((l.theta_to_r(th_prior) * l.fx - r_prior[i] * fac.fx) / prior_px * n_scale)
                pri.append(np.array([l.cx - fac.cx, l.cy - fac.cy]) / center_sigma_px * n_scale)
                # the curve must keep increasing through the overlap zone
                t2 = th_mono * th_mono
                k1, k2, k3, k4 = l.k
                slope = 1 + t2 * (3 * k1 + t2 * (5 * k2 + t2 * (7 * k3 + t2 * 9 * k4)))
                pri.append(np.minimum(slope - 0.2, 0) * 1000 * n_scale)
            return np.concatenate([ang] + [np.ravel(p) for p in pri])

        sol = least_squares(resid, _pack(cur) if it else _pack(factory) * 1.0, loss="soft_l1",
                            f_scale=3.0, x_scale="jac", max_nfev=200)
        cur = _unpack(sol.x, factory)
        ang = resid(sol.x)[:len(P0)]
        if verbose:
            print(f"           ray disagreement median {np.median(ang):.2f}px p90 {np.percentile(ang, 90):.2f}px, "
                  f"curve monotone to {_monotone_deg(cur):.0f} deg")
    meds = [correspondences(fr, cur, 4096, 2048, 8.0)[2] for fr in frames_list]
    if verbose:
        print(f"  final seam offset median {np.nanmedian(meds):.2f}px @2048")
    return cur
