"""Inject 360° (equirectangular, mono) metadata into an MP4/MOV.

Writes both flavours of the Google spatial-media spec so every player/editor
finds one it understands:
  * V1: `uuid` box (ffcc8263-f855-4a93-8814-587a02521fdd) with GSpherical XMP in the video trak
  * V2: `st3d` + `sv3d` boxes inside the video sample entry (hvc1/avc1/apch...)

Only files whose `moov` comes after `mdat` are supported (ffmpeg's default
without +faststart): the moov is rebuilt in place at the end of the file, so
no chunk offsets change.
"""
from __future__ import annotations

import struct
from pathlib import Path

V1_UUID = bytes.fromhex("ffcc8263f8554a938814587a02521fdd")
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
VISUAL_ENTRY_HEADER = 78  # bytes after the 8-byte box header in a VisualSampleEntry


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def _full(typ: bytes, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    return _box(typ, struct.pack(">I", (version << 24) | flags) + payload)


def v1_xmp(stitcher: str = "osv360") -> bytes:
    xml = (
        '<?xml version="1.0"?><rdf:SphericalVideo '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:GSpherical="http://ns.google.com/videos/1.0/spherical/">'
        "<GSpherical:Spherical>true</GSpherical:Spherical>"
        "<GSpherical:Stitched>true</GSpherical:Stitched>"
        f"<GSpherical:StitchingSoftware>{stitcher}</GSpherical:StitchingSoftware>"
        "<GSpherical:ProjectionType>equirectangular</GSpherical:ProjectionType>"
        "</rdf:SphericalVideo>"
    )
    return _box(b"uuid", V1_UUID + xml.encode())


def v2_boxes(stitcher: str = "osv360") -> bytes:
    st3d = _full(b"st3d", bytes([0]))  # monoscopic
    svhd = _full(b"svhd", stitcher.encode() + b"\0")
    prhd = _full(b"prhd", struct.pack(">iii", 0, 0, 0))  # yaw, pitch, roll
    equi = _full(b"equi", struct.pack(">IIII", 0, 0, 0, 0))  # full sphere
    sv3d = _box(b"sv3d", svhd + _box(b"proj", prhd + equi))
    return st3d + sv3d


def _children(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[pos:pos + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        yield typ, pos, size, hdr
        pos += size


def _rewrite(buf: bytes, typ: bytes, start: int, size: int, hdr: int, ctx: dict) -> bytes:
    """rebuild one box, recursing into containers on the video track."""
    body_start, end = start + hdr, start + size
    if typ == b"trak":
        hpos = buf.find(b"hdlr", body_start, end)
        is_video = hpos > 0 and buf[hpos + 12:hpos + 16] == b"vide"
        if not is_video or ctx["done"]:
            return buf[start:end]
        parts = []
        for ct, cp, cs, ch in _children(buf, body_start, end):
            if ct == b"uuid" and buf[cp + 8:cp + 24] == V1_UUID:
                continue  # replace an existing V1 box
            parts.append(_rewrite(buf, ct, cp, cs, ch, ctx))
            if ct == b"tkhd":
                parts.append(v1_xmp(ctx["stitcher"]))
        ctx["done"] = True
        return _box(b"trak", b"".join(parts))
    if typ in CONTAINERS:
        return _box(typ, b"".join(_rewrite(buf, ct, cp, cs, ch, ctx)
                                  for ct, cp, cs, ch in _children(buf, body_start, end)))
    if typ == b"stsd":
        head = buf[body_start:body_start + 8]  # version/flags + entry count
        entries = []
        for et, ep, es, eh in _children(buf, body_start + 8, end):
            fixed = buf[ep + eh:ep + eh + VISUAL_ENTRY_HEADER]
            kids = [buf[cp:cp + cs] for ct, cp, cs, ch in
                    _children(buf, ep + eh + VISUAL_ENTRY_HEADER, ep + es) if ct not in (b"st3d", b"sv3d")]
            entries.append(_box(et, fixed + b"".join(kids) + v2_boxes(ctx["stitcher"])))
        return _box(b"stsd", head + b"".join(entries))
    return buf[start:end]


def inject(path: str | Path, stitcher: str = "osv360") -> None:
    path = Path(path)
    with open(path, "r+b") as f:
        # locate top-level boxes without reading the whole (huge) file
        size_total = path.stat().st_size
        pos, moov = 0, None
        order = []
        while pos < size_total:
            f.seek(pos)
            hdr = f.read(16)
            size, typ = struct.unpack(">I4s", hdr[:8])
            if size == 1:
                size = struct.unpack(">Q", hdr[8:16])[0]
            elif size == 0:
                size = size_total - pos
            order.append(typ)
            if typ == b"moov":
                moov = (pos, size)
            pos += size
        if moov is None:
            raise ValueError("no moov box")
        if order.index(b"moov") < order.index(b"mdat"):
            raise ValueError("moov before mdat (faststart) is not supported")
        f.seek(moov[0])
        mbuf = f.read(moov[1])
        ctx = {"done": False, "stitcher": stitcher}
        new = _rewrite(mbuf, b"moov", 0, len(mbuf), 8, ctx)
        if not ctx["done"]:
            raise ValueError("no video track found")
        tail_start = moov[0] + moov[1]
        f.seek(tail_start)
        tail = f.read()  # boxes after moov (usually none)
        f.seek(moov[0])
        f.write(new + tail)
        f.truncate()
