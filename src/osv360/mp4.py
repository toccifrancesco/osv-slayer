"""Minimal ISO-BMFF (MP4/MOV) reader.

Only what is needed to locate track samples inside a DJI .OSV without
extracting anything to disk: the track table, per-track sample sizes and
absolute file offsets (stsz/stz2, stco/co64, stsc), timescale and handler.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta", b"dinf"}


@dataclass
class Box:
    type: bytes
    offset: int  # offset of the box header
    size: int  # total size including header
    header: int  # header length (8 or 16)

    @property
    def payload_offset(self) -> int:
        return self.offset + self.header

    @property
    def payload_size(self) -> int:
        return self.size - self.header


def iter_boxes(f: BinaryIO, start: int, end: int) -> Iterator[Box]:
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, typ = struct.unpack(">I4s", f.read(8))
        header = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            break
        yield Box(typ, pos, size, header)
        pos += size


@dataclass
class Track:
    track_id: int
    handler: str  # 'vide', 'soun', 'meta', ...
    codec: str  # sample entry fourcc: 'hvc1', 'mp4a', 'djmd', 'dbgi', ...
    handler_name: str
    timescale: int
    duration: int
    width: int = 0
    height: int = 0
    sample_sizes: list[int] = field(default_factory=list)
    sample_offsets: list[int] = field(default_factory=list)

    @property
    def sample_count(self) -> int:
        return len(self.sample_sizes)


class MP4:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.f = open(self.path, "rb")
        self.file_size = self.path.stat().st_size
        self.top: list[Box] = list(iter_boxes(self.f, 0, self.file_size))
        moov = next((b for b in self.top if b.type == b"moov"), None)
        if moov is None:
            raise ValueError(f"{path}: no moov box")
        self.tracks: list[Track] = [
            self._parse_trak(b)
            for b in iter_boxes(self.f, moov.payload_offset, moov.offset + moov.size)
            if b.type == b"trak"
        ]

    def close(self) -> None:
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ parsing
    def _read(self, box: Box) -> bytes:
        self.f.seek(box.payload_offset)
        return self.f.read(box.payload_size)

    def _find(self, parent: Box, path: list[bytes]) -> Box | None:
        cur = parent
        for t in path:
            nxt = None
            for b in iter_boxes(self.f, cur.payload_offset, cur.offset + cur.size):
                if b.type == t:
                    nxt = b
                    break
            if nxt is None:
                return None
            cur = nxt
        return cur

    def _parse_trak(self, trak: Box) -> Track:
        tkhd = self._read(self._find(trak, [b"tkhd"]))
        ver = tkhd[0]
        track_id = struct.unpack(">I", tkhd[12:16] if ver == 0 else tkhd[20:24])[0]
        width = struct.unpack(">I", tkhd[-8:-4])[0] >> 16
        height = struct.unpack(">I", tkhd[-4:])[0] >> 16

        mdhd = self._read(self._find(trak, [b"mdia", b"mdhd"]))
        if mdhd[0] == 1:
            timescale, duration = struct.unpack(">IQ", mdhd[20:32])
        else:
            timescale, duration = struct.unpack(">II", mdhd[12:20])

        hdlr = self._read(self._find(trak, [b"mdia", b"hdlr"]))
        handler = hdlr[8:12].decode("latin1")
        handler_name = hdlr[24:].split(b"\0")[0].decode("utf-8", "replace")

        stbl = self._find(trak, [b"mdia", b"minf", b"stbl"])
        stsd = self._read(self._find(stbl, [b"stsd"]))
        codec = stsd[12:16].decode("latin1") if len(stsd) >= 16 else ""

        sizes = self._sample_sizes(stbl)
        offsets = self._sample_offsets(stbl, len(sizes), sizes)
        return Track(track_id, handler, codec, handler_name, timescale, duration,
                     width, height, sizes, offsets)

    def _sample_sizes(self, stbl: Box) -> list[int]:
        b = self._find(stbl, [b"stsz"])
        if b is not None:
            d = self._read(b)
            fixed, count = struct.unpack(">II", d[4:12])
            if fixed:
                return [fixed] * count
            return list(struct.unpack(f">{count}I", d[12:12 + 4 * count]))
        b = self._find(stbl, [b"stz2"])
        d = self._read(b)
        field_size = d[7]
        count = struct.unpack(">I", d[8:12])[0]
        if field_size == 16:
            return list(struct.unpack(f">{count}H", d[12:12 + 2 * count]))
        if field_size == 8:
            return list(d[12:12 + count])
        raise ValueError("unsupported stz2 field size 4")

    def _sample_offsets(self, stbl: Box, n: int, sizes: list[int]) -> list[int]:
        b = self._find(stbl, [b"stco"])
        if b is not None:
            d = self._read(b)
            cnt = struct.unpack(">I", d[4:8])[0]
            chunks = list(struct.unpack(f">{cnt}I", d[8:8 + 4 * cnt]))
        else:
            d = self._read(self._find(stbl, [b"co64"]))
            cnt = struct.unpack(">I", d[4:8])[0]
            chunks = list(struct.unpack(f">{cnt}Q", d[8:8 + 8 * cnt]))
        d = self._read(self._find(stbl, [b"stsc"]))
        ecnt = struct.unpack(">I", d[4:8])[0]
        entries = [struct.unpack(">III", d[8 + 12 * i:20 + 12 * i]) for i in range(ecnt)]

        offsets: list[int] = []
        s = 0
        for ei, (first, per_chunk, _desc) in enumerate(entries):
            last = entries[ei + 1][0] - 1 if ei + 1 < len(entries) else len(chunks)
            for c in range(first - 1, last):
                off = chunks[c]
                for _ in range(per_chunk):
                    if s >= n:
                        return offsets
                    offsets.append(off)
                    off += sizes[s]
                    s += 1
        return offsets

    # ------------------------------------------------------------------ access
    def tracks_by_codec(self, codec: str) -> list[Track]:
        return [t for t in self.tracks if t.codec == codec]

    def read_sample(self, track: Track, i: int) -> bytes:
        self.f.seek(track.sample_offsets[i])
        return self.f.read(track.sample_sizes[i])

    def iter_samples(self, track: Track) -> Iterator[bytes]:
        for i in range(track.sample_count):
            yield self.read_sample(track, i)
