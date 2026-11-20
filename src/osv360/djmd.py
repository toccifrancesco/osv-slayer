"""Decoder for the DJI Osmo 360 `djmd` metadata track (protobuf, dvtm_oq101.proto).

No .proto file is published, so this is a schema-less protobuf walker plus a
field map established by inspection (see README, "Metadata"):

  packet 0 only:
    1.1.1  proto name ("dvtm_oq101.proto")     1.1.10 model ("Osmo 360")
    1.10.1 IMU rate (Hz, 1000)                 1.11.1 frame rate (49.9985)
    2.3.{1,2,3} width, height, fps
    2.6.N  lens calibration entries; N=1,2 are the two lenses for the active mode:
           1 fx, 2 fy, 3 cx, 4 cy, 5..8 k1..k4, 10/11 calib size,
           12/13/14 lens yaw/pitch/roll (deg), 21 & 28 lens->body quaternion (xyzw)
  every packet (one per video frame):
    3.1.2  frame timestamp (us)
    3.2.9  attitude quaternion at frame time (w, x, y, z)
    3.3.2.1.1 timestamp (us) of the first IMU sample in this packet
    3.3.2.1.3 (repeated) attitude quaternions at 1 kHz (w, x, y, z)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .mp4 import MP4


# ------------------------------------------------------------------ protobuf walker
def _varint(b: bytes, o: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[o]
        o += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, o


def fields(b: bytes) -> list[tuple[int, int, object]]:
    """flat list of (field, wiretype, value) for one message level."""
    out = []
    o = 0
    n = len(b)
    while o < n:
        key, o = _varint(b, o)
        f, w = key >> 3, key & 7
        if w == 0:
            v, o = _varint(b, o)
        elif w == 1:
            v = b[o:o + 8]
            o += 8
        elif w == 5:
            v = b[o:o + 4]
            o += 4
        elif w == 2:
            ln, o = _varint(b, o)
            v = b[o:o + ln]
            o += ln
        else:
            raise ValueError(f"unsupported wire type {w}")
        out.append((f, w, v))
    return out


def get(b: bytes, *path: int, default=None):
    """first value at a nested field path."""
    cur = b
    for p in path:
        for f, _w, v in fields(cur):
            if f == p:
                cur = v
                break
        else:
            return default
    return cur


def get_all(b: bytes, field: int) -> list:
    return [v for f, _w, v in fields(b) if f == field]


def f32(v: bytes) -> float:
    return struct.unpack("<f", v)[0]


def floats(v: bytes) -> list[float]:
    """message of fixed32 floats keyed 1..n -> list ordered by field number."""
    d = {f: f32(x) for f, w, x in fields(v) if w == 5}
    return [d[k] for k in sorted(d)]


# ------------------------------------------------------------------ data model
@dataclass
class LensCalib:
    fx: float
    fy: float
    cx: float
    cy: float
    k: list[float]
    ypr_deg: list[float]
    q_body_xyzw: list[float]
    calib_size: tuple[int, int]


@dataclass
class OSVMeta:
    model: str
    proto: str
    serial: str
    imu_rate: int
    fps: float
    lenses: list[LensCalib]
    frame_ts_us: np.ndarray  # (N,) int64
    frame_quat_wxyz: np.ndarray  # (N,4) attitude at frame time
    imu_ts_us: np.ndarray  # (M,) int64
    imu_quat_wxyz: np.ndarray  # (M,4)


def _lens(entry: bytes) -> LensCalib:
    d = {f: (w, v) for f, w, v in fields(entry)}

    def fv(n):
        return f32(d[n][1])

    q = d.get(28, d.get(21))[1]
    qv = floats(q) if q and len(q) != 16 else list(np.frombuffer(q, "<f4").astype(float))
    return LensCalib(fv(1), fv(2), fv(3), fv(4), [fv(5), fv(6), fv(7), fv(8)],
                     [fv(12), fv(13), fv(14)], [float(x) for x in qv],
                     (int(fv(10)), int(fv(11))))


def read_meta(path: str | Path, track: int = 0) -> OSVMeta:
    with MP4(path) as m:
        tr = m.tracks_by_codec("djmd")[track]
        first = m.read_sample(tr, 0)
        hdr = get(first, 1, 1)
        cal = get(first, 2, 6)
        entries = {f: v for f, w, v in fields(cal) if w == 2}
        lenses = [_lens(entries[1]), _lens(entries[2])]
        imu_rate = get(first, 1, 10, 1, default=1000)
        fps = f32(get(first, 1, 11, 1)) if get(first, 1, 11, 1) else 50.0

        fts, fq, its, iq = [], [], [], []
        for pk in m.iter_samples(tr):
            fts.append(get(pk, 3, 1, 2))
            q = get(pk, 3, 2, 9)
            fq.append(floats(q) if q else [1.0, 0, 0, 0])
            blk = get(pk, 3, 3, 2, 1)
            if blk is None:
                continue
            t0 = get(blk, 1)
            qs = [floats(v) for v in get_all(blk, 3)]
            n = len(qs)
            step = 1_000_000 // imu_rate
            its.extend(t0 + step * i for i in range(n))
            iq.extend(qs)

    return OSVMeta(
        model=get(hdr, 10, default=b"").decode(),
        proto=get(hdr, 1, default=b"").decode(),
        serial=get(hdr, 5, default=b"").decode(),
        imu_rate=imu_rate,
        fps=fps,
        lenses=lenses,
        frame_ts_us=np.asarray(fts, np.int64),
        frame_quat_wxyz=np.asarray(fq, np.float64),
        imu_ts_us=np.asarray(its, np.int64),
        imu_quat_wxyz=np.asarray(iq, np.float64),
    )
