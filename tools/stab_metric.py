"""Measure how well an equirectangular video is stabilized / horizon-locked.

For consecutive sampled frames the camera rotation *visible in the output* is
estimated from feature tracks (robust spherical relative pose, so forward
motion/parallax does not leak into the rotation). It is split into the output's
own axes:  roll (about the forward axis), pitch (about the right axis) and yaw
(about the vertical). A perfectly horizon-locked video has roll = pitch = 0
for every step; yaw is whatever the heading-follow does.

Also reports the accumulated roll/pitch drift: summing the per-step tilt
rotations shows whether the horizon slowly wanders.

usage: stab_metric.py VIDEO [--fps 10] [--start 0] [--duration 60] [--csv out.csv]
"""
import argparse
import subprocess

import numpy as np

from osv360.vision import track_rotation

ap = argparse.ArgumentParser()
ap.add_argument("video")
ap.add_argument("--fps", type=float, default=10)
ap.add_argument("--start", type=float, default=0)
ap.add_argument("--duration", type=float, default=60)
ap.add_argument("--csv")
a = ap.parse_args()
W, H = 1024, 512
# exact frame subsampling (the fps filter picks frames up to half an output interval away)
probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                        "-of", "csv=p=0", a.video], capture_output=True, text=True, check=True).stdout.strip()
num, den = (int(x) for x in probe.strip(",").split("/"))
step = max(1, round(num / den / a.fps))
raw = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", str(a.start), "-t", str(a.duration), "-i", a.video,
                        "-vf", f"select=not(mod(n\\,{step})),scale={W}:{H}", "-fps_mode", "passthrough",
                        "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"], stdout=subprocess.PIPE)
lat = (0.5 - (np.arange(H) + 0.5) / H) * 180
mask = np.zeros((H, W), np.uint8)
mask[(lat > -25) & (lat < 70)] = 255  # skip the camera mount / helmet near the nadir

steps = []
prev = None
while True:
    buf = raw.stdout.read(W * H)
    if len(buf) < W * H:
        break
    cur = np.frombuffer(buf, np.uint8).reshape(H, W)
    if prev is not None:
        r = track_rotation(prev, cur, mask)
        steps.append(r.as_rotvec() if r is not None else np.full(3, np.nan))
    prev = cur
s = np.degrees(np.array(steps))  # view frame: x right (pitch), y down (yaw), z forward (roll)
ok = np.isfinite(s).all(1)
s = s[ok]
pitch, yaw, roll = s[:, 0], s[:, 1], s[:, 2]
rate = num / den / step


def rms(x):
    return float(np.sqrt(np.mean(x ** 2)))


print(f"{a.video}: {len(s)} steps @ {rate:g} fps")
print(f"  per-step |roll|  RMS {rms(roll):.3f} deg  ({rms(roll) * rate:.2f} deg/s)   p95 {np.percentile(np.abs(roll), 95):.3f}")
print(f"  per-step |pitch| RMS {rms(pitch):.3f} deg  ({rms(pitch) * rate:.2f} deg/s)   p95 {np.percentile(np.abs(pitch), 95):.3f}")
print(f"  per-step |yaw|   RMS {rms(yaw):.3f} deg  ({rms(yaw) * rate:.2f} deg/s)")
acc_r, acc_p = np.cumsum(roll), np.cumsum(pitch)
print(f"  accumulated drift: roll range {np.ptp(acc_r):.2f} deg, pitch range {np.ptp(acc_p):.2f} deg")
if a.csv:
    np.savetxt(a.csv, np.c_[np.arange(len(s)) / rate, roll, pitch, yaw], delimiter=",",
               header="t,roll,pitch,yaw", comments="")
