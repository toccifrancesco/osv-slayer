"""Estimate the rolling-shutter readout time from footage + IMU.

Features are tracked inside each fisheye between consecutive frames during a
fast-rotation segment. Each observation is unprojected and rotated into the
world using the IMU attitude at that *row's* exposure time
(t_frame + (v/H - 0.5) * readout). The readout minimising the disagreement
between the two world rays is the estimate.

usage: estimate_readout.py OSV rig.json start_s [duration_s=2]
"""
import subprocess
import sys

import cv2
import numpy as np
from scipy.spatial.transform import Rotation as R

from osv360.djmd import read_meta
from osv360.lens import Rig
from osv360.motion import Attitude

osv, rigp, t0 = sys.argv[1], sys.argv[2], float(sys.argv[3])
dur = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
S = 1280
rig = Rig.load(rigp)
rs = rig.scaled(S)
meta = read_meta(osv)
att = Attitude(meta.imu_ts_us, meta.imu_quat_wxyz)
body_from_imu = R.from_quat(rig.q_body_from_imu)
fps = 50.0
i0 = int(round(t0 * fps))
n = int(round(dur * fps))


def frames(stream):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-hwaccel", "vaapi", "-ss", f"{i0 / fps:.3f}", "-i", osv,
                          "-map", f"0:{stream}", "-frames:v", str(n), "-vf", f"scale={S}:{S}", "-pix_fmt", "gray",
                          "-f", "rawvideo", "pipe:1"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, S, S)


obs = []  # (lens, frame_i, uv_i, uv_j)
for k in (0, 1):
    F = frames(k)
    mask = np.zeros((S, S), np.uint8)
    cv2.circle(mask, (S // 2, S // 2), int(S * 0.47), 255, -1)
    for i in range(len(F) - 1):
        p0 = cv2.goodFeaturesToTrack(F[i], 600, 0.01, 10, mask=mask)
        if p0 is None:
            continue
        p1, st, _ = cv2.calcOpticalFlowPyrLK(F[i], F[i + 1], p0, None, winSize=(21, 21), maxLevel=3)
        ok = st.ravel() == 1
        for a, b in zip(p0.reshape(-1, 2)[ok], p1.reshape(-1, 2)[ok]):
            obs.append((k, i, a, b))
print(f"{len(obs)} feature tracks")
lens_idx = np.array([o[0] for o in obs])
fi = np.array([o[1] for o in obs])
A = np.array([o[2] for o in obs])
B = np.array([o[3] for o in obs])
ts = meta.frame_ts_us * 1e-6 + rig.imu_offset_s

rays_a = np.zeros((len(obs), 3))
rays_b = np.zeros((len(obs), 3))
for k in (0, 1):
    m = lens_idx == k
    L = rs.lenses[k]
    rays_a[m] = L.rot.apply(L.unproject(A[m]))  # body frame
    rays_b[m] = L.rot.apply(L.unproject(B[m]))


SCAN = 1  # 1: rows (v), 0: columns (u)


def residual(ro):
    """after IMU rotation compensation only translation remains: per frame pair fit the
    translation direction t with (wa x wb) . t = 0, score = median epipolar error."""
    ta = ts[i0 + fi] + (A[:, SCAN] / S - 0.5) * ro
    tb = ts[i0 + fi + 1] + (B[:, SCAN] / S - 0.5) * ro
    wa = (att.at(ta) * body_from_imu.inv()).apply(rays_a)
    wb = (att.at(tb) * body_from_imu.inv()).apply(rays_b)
    errs = []
    for i in np.unique(fi):
        m = fi == i
        c = np.cross(wa[m], wb[m])
        nrm = np.linalg.norm(c, axis=1)
        good = nrm > 1e-6
        if good.sum() < 30:
            continue
        cn = c[good] / nrm[good, None]
        w = np.ones(good.sum())
        for _ in range(4):  # IRLS: robust to independently moving objects
            t = np.linalg.svd(cn * w[:, None])[2][-1]
            e = np.abs(cn @ t)
            w = 1 / np.maximum(e, 1e-4)
        # epipolar distance in angle ~ |(wa x wb).t| (small motions)
        errs.append(np.abs(c[good] @ t))
    e = np.concatenate(errs)
    return np.degrees(np.median(e)), len(e)


for SCAN, name in ((1, "rows"), (0, "columns")):
    res = [(residual(x / 1000)[0], x) for x in np.arange(-40, 40.1, 4.0)]
    print(name, " ".join(f"{x:+.0f}:{r * 60:.2f}" for r, x in res))
    r, x = min(res)
    fine = [(residual(y / 1000)[0], y) for y in np.arange(x - 4, x + 4.01, 0.5)]
    r, x = min(fine)
    print(f"  best {name}: readout {x:+.1f} ms, residual {r * 60:.2f} arcmin")
