import cv2
import numpy as np
import pandas as pd
from scipy.spatial.transform import Rotation as R
from scipy.interpolate import interp1d
from tqdm import tqdm


VIDEO_FILE = "3_360.mp4"
IMU_FILE = "imu.csv"
OUTPUT_FILE = "horizon_locked.mp4"


# =========================
# Load IMU data
# =========================

imu = pd.read_csv(IMU_FILE)

imu_times = imu["timestamp"].values
quats = imu[["qx", "qy", "qz", "qw"]].values

# scipy wants quaternions as x,y,z,w
rotations = R.from_quat(quats)

# Convert to Euler
# yaw, pitch, roll
# We will KEEP yaw but remove roll and pitch
# to maintain horizon lock

euler = rotations.as_euler("zyx", degrees=False)

# euler[:,0] = yaw
# euler[:,1] = pitch
# euler[:,2] = roll

# Keep only yaw
corrected_euler = np.zeros_like(euler)
corrected_euler[:, 0] = euler[:, 0]

# Convert back to rotations
corrected_rotations = R.from_euler("zyx", corrected_euler)

# Interpolators
quat_interp = interp1d(
    imu_times,
    corrected_rotations.as_quat(),
    axis=0,
    bounds_error=False,
    fill_value="extrapolate"
)


# =========================
# Open video
# =========================

cap = cv2.VideoCapture(VIDEO_FILE)

fps = cap.get(cv2.CAP_PROP_FPS)
width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

fourcc = cv2.VideoWriter_fourcc(*"mp4v")
out = cv2.VideoWriter(OUTPUT_FILE, fourcc, fps, (width, height))


# =========================
# Equirectangular remap helpers
# =========================


x_coords = np.linspace(-np.pi, np.pi, width)
y_coords = np.linspace(np.pi / 2, -np.pi / 2, height)

lon, lat = np.meshgrid(x_coords, y_coords)

# Sphere coordinates
X = np.cos(lat) * np.cos(lon)
Y = np.sin(lat)
Z = np.cos(lat) * np.sin(lon)

sphere = np.stack([X, Y, Z], axis=-1)


# =========================
# Process frames
# =========================

for frame_idx in tqdm(range(frame_count)):

    ret, frame = cap.read()
    if not ret:
        break

    t = frame_idx / fps

    q = quat_interp(t)
    rot = R.from_quat(q)

    # Apply inverse rotation
    rotated = rot.inv().apply(sphere.reshape(-1, 3))
    rotated = rotated.reshape(height, width, 3)

    Xr = rotated[:, :, 0]
    Yr = rotated[:, :, 1]
    Zr = rotated[:, :, 2]

    lon_r = np.arctan2(Zr, Xr)
    lat_r = np.arcsin(Yr)
    map_x = ((lon_r + np.pi) / (2 * np.pi) * width).astype(np.float32)
    map_y = ((np.pi / 2 - lat_r) / np.pi * height).astype(np.float32)

    stabilized = cv2.remap(
        frame,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_WRAP
    )

    out.write(stabilized)


cap.release()
out.release()

print("Done ->", OUTPUT_FILE)
