"""Camera motion from the 1 kHz IMU attitude + stabilization modes.

Frames:
  * IMU frame: frame of the attitude quaternions in the djmd track.
  * body frame: lens rig frame (see lens.py).  R_body_from_imu is a fixed
    mounting rotation (estimated once by tools/imu_align.py, stored in the rig).
  * world frame: gravity-aligned frame of the attitude filter (+z DOWN).
  * view frame: output equirect frame (x right, y down, z forward).

attitude q(t): world_from_imu (checked against the footage in imu_align).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.spatial.transform import Rotation as R
from scipy.spatial.transform import Slerp

# The attitude world frame is NED-like (+z DOWN, verified against the footage).
# view (x right, y down, z forward) at zero heading: forward = world +x, right = world +y.
VIEW_FROM_WORLD0 = R.from_matrix([[0, 1, 0], [0, 0, 1], [1, 0, 0]])


class Attitude:
    def __init__(self, ts_us: np.ndarray, quat_wxyz: np.ndarray):
        t = ts_us.astype(np.float64) * 1e-6
        # enforce strictly increasing timestamps (block rebasing may create ties)
        keep = np.r_[True, np.diff(t) > 0]
        q = quat_wxyz[keep]
        self.t = t[keep]
        self.rot = R.from_quat(q[:, [1, 2, 3, 0]])  # world_from_imu
        self._slerp = Slerp(self.t, self.rot)

    def at(self, t_s: np.ndarray) -> R:
        t = np.clip(np.asarray(t_s, np.float64), self.t[0], self.t[-1])
        return self._slerp(t)


def heading_of(world_from_body: R, forward_body: np.ndarray) -> np.ndarray:
    """yaw (rad, about world +z) of the body's forward axis projected on the horizon."""
    f = world_from_body.apply(np.broadcast_to(forward_body, (len(world_from_body), 3)))
    return np.arctan2(f[:, 1], f[:, 0])


@dataclass
class StabParams:
    mode: str = "horizon"  # horizon | lock | smooth | none
    yaw_smooth_s: float = 1.0  # gaussian sigma of the heading follow (horizon mode)
    smooth_s: float = 0.5  # sigma for 'smooth' (full rotation smoothing)


def view_rotations(world_from_body: R, fps: float, forward_body: np.ndarray,
                   params: StabParams) -> R:
    """per-frame body_from_view rotation used by the stitcher.

    horizon: the view is gravity-levelled; its heading follows the camera's
             heading through a zero-phase gaussian (no roll/pitch at all).
    lock:    gravity-levelled, heading frozen to the first frame.
    smooth:  full 3-axis rotation low-passed (keeps intentional tilts).
    none:    view rigidly attached to the camera body.
    """
    n = len(world_from_body)
    if params.mode == "none":
        return R.from_quat(np.tile([0, 0, 0, 1.0], (n, 1)))  # identity: caller applies rig default
    if params.mode == "smooth":
        q = world_from_body.as_quat()
        flips = np.sign((q[1:] * q[:-1]).sum(1))  # keep consecutive quats in one hemisphere
        q = np.cumprod(np.r_[1.0, np.where(flips == 0, 1.0, flips)])[:, None] * q
        qs = gaussian_filter1d(q, params.smooth_s * fps, axis=0, mode="nearest")
        world_from_view_body = R.from_quat(qs / np.linalg.norm(qs, axis=1, keepdims=True))
        # body_from_view = body_from_world * world_from_smoothedbody
        return world_from_body.inv() * world_from_view_body
    yaw = np.unwrap(heading_of(world_from_body, forward_body))
    if params.mode == "lock":
        yaw = np.full(n, yaw[0])
    else:
        yaw = gaussian_filter1d(yaw, max(params.yaw_smooth_s, 1e-3) * fps, mode="nearest")
    world_from_view = R.from_euler("z", yaw[:, None]) * VIEW_FROM_WORLD0.inv()
    return world_from_body.inv() * world_from_view


def body_attitude(att: Attitude, frame_ts_us: np.ndarray, body_from_imu: R, offset_s: float,
                  dt_s: float = 0.0) -> R:
    """world_from_body at each frame time (+ dt_s, e.g. for rolling-shutter rows)."""
    t = frame_ts_us.astype(np.float64) * 1e-6 + offset_s + dt_s
    return att.at(t) * body_from_imu.inv()
