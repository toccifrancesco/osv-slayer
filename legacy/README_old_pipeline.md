# DJI 360° Video Stabilization Pipeline

Stabilize 360° videos shot with DJI cameras by extracting onboard IMU data and applying horizon-locked rotation correction.

## Pipeline

1. **Capture** — DJI camera records 360° equirectangular video
2. **Extract** — unpack `.OSV/.LRF` video archives to raw binary payloads
3. **Decode IMU** — parse DJI proprietary binary format to extract quaternion rotation data (800 Hz)
4. **Stabilize** — apply inverse yaw-only rotation to each video frame via spherical remapping

## Files

| File | Description |
|------|-------------|
| `*.OSV` | DJI video archive (~2.5 GB each) |
| `*.LRF` | DJI sidecar metadata (~130 MB each) |
| `CAM_*.mp4` | Decoded 360° video from archive |

`chat/`

| File | Description |
|------|-------------|
| `decode_dji.py` | Extracts IMU quaternions from DJMD binary payloads |
| `horizon_lock.py` | Stabilizes 360° video using extracted IMU data |
| `imu.csv` | Extracted IMU data (timestamp, qw, qx, qy, qz) |
| `decode-env/` | Python 3.14 virtualenv with dependencies |
| `horizon_locked.mp4` | Final stabilized output |
| `3_360.mp4` | Input 360° video (clip 3) |
| `3-track*.mp4` | Split tracks from 360° video |
| `3-dbgi*.bin` | DJI database containers |
| `3-djmd*.bin` | DJI IMU/motion data binaries |
| `3-additional-boxes.bin` | Additional DJI payload data |

## Prerequisites

- Python 3.14
- `cv2` (OpenCV)
- `numpy`, `pandas`, `scipy`
- `tqdm`

## Usage

```bash
# 1. Extract IMU data from DJI binary
source chat/decode-env/bin/activate
cd chat
python decode_dji.py 3-djmd1.bin 3-djmd1.bin.sizes

# 2. Stabilize the video
python horizon_lock.py
```

`horizon_lock.py` reads `imu.csv` and `3_360.mp4`, outputs `horizon_locked.mp4`.

## How It Works

### IMU Decoder (`decode_dji.py`)

- Reads binary packet sizes from `.sizes` sidecar file
- Iterates through DJI binary packets, parsing Varint-encoded protobuf-like structure
- Extracts quaternion payloads (wire type 2, field 3, 20 bytes with nested float32 fields)
- Outputs CSV at 800 Hz sample rate compatible with Gyroflow

### Horizon Lock (`horizon_lock.py`)

1. Loads IMU quaternions, converts to Euler angles (zyx)
2. Keeps only yaw, zeroing pitch and roll
3. Converts back to quaternions with interpolation via `scipyp.interpolate.interp1d`
4. For each video frame:
   - Get interpolated quaternion at frame timestamp
   - Define a sphere mesh from equirectangular coordinates
   - Apply inverse rotation to the sphere
   - Map back to screen space and use `cv2.remap` with bilinear interpolation
   - Write stabilized frame

Result: a stabilized 360° video where the horizon remains level throughout
