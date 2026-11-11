"""
Vanta IDE — icon generator.

Pure stdlib. Rasterizes the VANTA mark (the V from the app's own SVG) onto a
dark rounded tile and writes a multi-size .ico with PNG-compressed frames.

    python ide/tools/make_icon.py [out.ico]

No PIL, no cairo — the V is a band between two triangles, sampled 4x4 per pixel.
"""

from __future__ import annotations

import os
import struct
import sys
import zlib

SIZES = (256, 128, 64, 48, 32, 16)

BG = (10, 12, 15)          # #0a0c0f — editor background
EDGE = (31, 42, 51)        # subtle rim
MARK = (61, 219, 208)      # #3ddbd0 — cursor / accent
MARK_DIM = (28, 122, 118)  # lower half of the V, slight falloff

OUTER = ((3.0, 4.0), (21.0, 4.0), (12.0, 21.0))
INNER = ((6.4, 4.0), (12.0, 16.2), (17.6, 4.0))


def _in_tri(px: float, py: float, tri) -> bool:
    (ax, ay), (bx, by), (cx, cy) = tri
    d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
    d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
    d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def _in_round_rect(px: float, py: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    if px == cx or py == cy:
        return True
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def render(size: int) -> bytes:
    """Return raw RGBA rows for one square frame."""
    ss = 4                      # supersample factor per axis
    pad = size * 0.03           # tile inset
    radius = size * 0.22
    x0, y0, x1, y1 = pad, pad, size - pad, size - pad
    # V occupies the classic 24-unit box, centered with breathing room
    box = (size - 2 * pad) * 0.70
    off_x = (size - box) / 2 - (3.0 / 24.0) * box
    off_y = (size - box) / 2 - (3.0 / 24.0) * box

    def to_px(ux: float, uy: float):
        return off_x + ux / 24.0 * box, off_y + uy / 24.0 * box

    outer = tuple(to_px(*p) for p in OUTER)
    inner = tuple(to_px(*p) for p in INNER)

    # optically center the visible V, not the 24-unit design box
    xs = [p[0] for p in outer]
    ys = [p[1] for p in outer]
    dx = (size - (max(xs) - min(xs))) / 2 - min(xs)
    dy = (size - (max(ys) - min(ys))) / 2 - min(ys)
    outer = tuple((x + dx, y + dy) for x, y in outer)
    inner = tuple((x + dx, y + dy) for x, y in inner)

    rows = bytearray()
    inv = 1.0 / (ss * ss)
    for py in range(size):
        rows.append(0)  # PNG filter byte: none
        for px in range(size):
            tile_hits = 0
            edge_hits = 0
            mark_hits = 0
            mark_top = 0
            for sy in range(ss):
                fy = py + (sy + 0.5) / ss
                for sx in range(ss):
                    fx = px + (sx + 0.5) / ss
                    if _in_round_rect(fx, fy, x0, y0, x1, y1, radius):
                        tile_hits += 1
                        if not _in_round_rect(fx, fy, x0 + 1.6, y0 + 1.6, x1 - 1.6, y1 - 1.6, radius * 0.94):
                            edge_hits += 1
                        if (_in_tri(fx, fy, outer) and not _in_tri(fx, fy, inner)):
                            mark_hits += 1
                            if fy < size * 0.5:
                                mark_top += 1
            a = tile_hits * inv
            if a <= 0.0:
                rows += b'\x00\x00\x00\x00'
                continue
            if mark_hits:
                mk = mark_hits * inv
                t = mark_top / mark_hits
                col = tuple(int(MARK[i] * t + MARK_DIM[i] * (1 - t)) for i in range(3))
                base = tuple(int(EDGE[i] * (edge_hits * inv) + BG[i] * (1 - edge_hits * inv)) for i in range(3))
                r = int(col[0] * mk + base[0] * (1 - mk))
                g = int(col[1] * mk + base[1] * (1 - mk))
                b = int(col[2] * mk + base[2] * (1 - mk))
            else:
                e = edge_hits * inv
                r = int(EDGE[0] * e + BG[0] * (1 - e))
                g = int(EDGE[1] * e + BG[1] * (1 - e))
                b = int(EDGE[2] * e + BG[2] * (1 - e))
            rows += bytes((r, g, b, int(255 * a)))
    return bytes(rows)


def png(size: int) -> bytes:
    raw = render(size)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def build(path: str):
    frames = [(s, png(s)) for s in SIZES]
    header = struct.pack('<HHH', 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries, blobs = b'', b''
    for size, data in frames:
        dim = 0 if size >= 256 else size
        entries += struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'wb') as f:
        f.write(header + entries + blobs)
    return path, sum(len(d) for _, d in frames)


if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'vanta.ico')
    p, n = build(target)
    print(f'wrote {p} ({n} bytes, {len(SIZES)} sizes)')
