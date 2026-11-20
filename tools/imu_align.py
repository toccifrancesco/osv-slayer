r"""Estimate the IMU -> lens-body mounting rotation and the IMU/video time offset.

Visual rotations between frames k*dt apart are measured on the stitched sphere
(LK feature tracks -> rays -> robust Kabsch), then matched against the IMU
attitude (hand-eye: A_vis = X * B_imu * X^-1).

usage: imu_align.py OSV rig.json lo_0.gray lo_1.gray [size=960] [fps=10]

lo_*.gray must hold every Nth source frame EXACTLY (N = 50/fps), e.g.
  ffmpeg -i X.OSV -map 0:0 -t 60 -vf "select=not(mod(n\,5)),scale=960:960" -fps_mode passthrough \
         -pix_fmt gray -f rawvideo lo_0.gray
(the `fps=` filter picks frames up to 2 source frames away and biases the offset by 40 ms).
"""
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation as R

from osv360.cpu_ref import stitch_maps
from osv360.djmd import read_meta
from osv360.lens import Rig, equirect_rays
from osv360.motion import Attitude
from osv360.vision import kabsch, rel_pose

osv, rigp, f0, f1 = sys.argv[1:5]
size = int(sys.argv[5]) if len(sys.argv) > 5 else 960
sfps = float(sys.argv[6]) if len(sys.argv) > 6 else 10.0
EW, EH = 1024, 512

rig = Rig.load(rigp).scaled(size)
L0 = np.memmap(f0, np.uint8, "r").reshape(-1, size, size)
L1 = np.memmap(f1, np.uint8, "r").reshape(-1, size, size)
n = min(len(L0), len(L1))
maps, w0, _ = stitch_maps(rig, EW, EH)  # body frame view (rig default)
rays = equirect_rays(EW, EH)
view_from_body = rig.view_from_body
lat = (0.5 - (np.arange(EH) + 0.5) / EH) * 180
valid = np.zeros((EH, EW), np.uint8)
valid[(lat > -30) & (lat < 75)] = 255  # skip mount/helmet and the zenith


def frame(i):
    a = cv2.remap(L0[i], *maps[0], cv2.INTER_LINEAR)
    b = cv2.remap(L1[i], *maps[1], cv2.INTER_LINEAR)
    return (a * w0 + b * (1 - w0)).astype(np.uint8)


def ray_at(p):
    x, y = p[:, 0], p[:, 1]
    lon = ((x + 0.5) / EW * 2 - 1) * np.pi
    la = (0.5 - (y + 0.5) / EH) * np.pi
    v = np.stack([np.cos(la) * np.sin(lon), -np.sin(la), np.cos(la) * np.cos(lon)], -1)
    return view_from_body.inv().apply(v)  # -> body frame


vis = []  # (i, j, R_body_j_from_body_i)
prev = frame(0)
for i in range(1, n):
    cur = frame(i)
    p0 = cv2.goodFeaturesToTrack(prev, 800, 0.01, 8, mask=valid)
    if p0 is None:
        prev = cur
        continue
    p1, st, _ = cv2.calcOpticalFlowPyrLK(prev, cur, p0, None, winSize=(21, 21), maxLevel=3)
    ok = st.ravel() == 1
    a, b = ray_at(p0.reshape(-1, 2)[ok]), ray_at(p1.reshape(-1, 2)[ok])
    w = np.ones(len(a))
    for _ in range(5):  # IRLS against moving objects
        M = kabsch(a, b, w)
        err = np.linalg.norm(b - a @ M.T, axis=1)
        w = 1 / np.maximum(err, 2e-3)
    M = rel_pose(a, b, R.from_matrix(kabsch(a, b, w))).as_matrix()
    # b = M a means a ray fixed in the world moves from body_i coords to body_j coords
    vis.append((i - 1, i, R.from_matrix(M)))
    prev = cur
print(f"visual rotations: {len(vis)} pairs")

meta = read_meta(osv)
att = Attitude(meta.imu_ts_us, meta.imu_quat_wxyz)
step = int(round(meta.fps / sfps))  # source frames per sampled frame
fts = meta.frame_ts_us * 1e-6

A = R.concatenate([v[2] for v in vis])
ang_vis = A.magnitude()


def imu_rel(off):
    ti = np.array([fts[v[0] * step] for v in vis]) + off
    tj = np.array([fts[v[1] * step] for v in vis]) + off
    qi, qj = att.at(ti), att.at(tj)  # world_from_imu
    return qi, qj


best = None
for off in np.arange(-0.10, 0.1001, 0.002):
    qi, qj = imu_rel(off)
    for inv in (False, True):
        # relative rotation in the IMU frame: imu_j_from_imu_i
        B = (qj.inv() * qi) if not inv else (qj * qi.inv())
        c = np.corrcoef(B.magnitude(), ang_vis)[0, 1]
        if best is None or c > best[0]:
            best = (c, off, inv)
c, off, inv = best
print(f"best time offset {off * 1000:+.1f} ms  (corr {c:.3f}), quaternion is "
      f"{'imu_from_world' if inv else 'world_from_imu'}")
qi, qj = imu_rel(off)
B = (qj.inv() * qi) if not inv else (qj * qi.inv())
# hand-eye on rotation vectors: rotvec(A) = X rotvec(B)
ra, rb = A.as_rotvec(), B.as_rotvec()
sel = (np.linalg.norm(ra, axis=1) > np.radians(0.5)) & (np.linalg.norm(rb, axis=1) > np.radians(0.5))
X = R.from_matrix(kabsch(rb[sel], ra[sel], np.linalg.norm(rb[sel], axis=1)))
res = np.degrees(np.linalg.norm(ra[sel] - X.apply(rb[sel]), axis=1))
print(f"pairs used {sel.sum()}, residual median {np.median(res):.3f} deg  p90 {np.percentile(res, 90):.3f}")
print("R_body_from_imu matrix:\n", np.round(X.as_matrix(), 4))
print("quat xyzw:", np.round(X.as_quat(), 6).tolist())
Path(".cache/work").mkdir(parents=True, exist_ok=True)
np.save(".cache/work/imu_align.npy", np.r_[X.as_quat(), off, float(inv)])

# ---- joint refinement: offset by minimising the hand-eye residual
def resid_for(off, X=None):
    qi, qj = imu_rel(off)
    B = qj.inv() * qi
    ra, rb = A.as_rotvec(), B.as_rotvec()
    sel = (np.linalg.norm(ra, axis=1) > np.radians(0.5))
    Xm = R.from_matrix(kabsch(rb[sel], ra[sel], np.linalg.norm(rb[sel], axis=1))) if X is None else X
    r = np.degrees(np.linalg.norm(ra[sel] - Xm.apply(rb[sel]), axis=1))
    return np.median(r), Xm, np.degrees(np.median(np.linalg.norm(ra[sel], axis=1)))
scan = [(resid_for(o)[0], o) for o in np.arange(-0.1, 0.1, 0.001)]
m, off2 = min(scan)
r2, X2, typ = resid_for(off2)
print(f"joint: offset {off2*1000:+.1f} ms residual median {r2:.3f} deg (typical rotation {typ:.2f} deg)")
for o in (-0.02, 0, 0.02, 0.048):
    print(f"   offset {o*1000:+.0f} ms -> {resid_for(o)[0]:.3f} deg")
print("quat xyzw:", np.round(X2.as_quat(), 6).tolist())
Path(".cache/work").mkdir(parents=True, exist_ok=True)
np.save(".cache/work/imu_align.npy", np.r_[X2.as_quat(), off2, 0.0])
