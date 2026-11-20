"""COBS（Consistent Overhead Byte Stuffing）。区切りは 0x00。

encode/decode はフレームの区切り 0x00 を含まない。区切りは frames.py が付け外しする。
254 バイトの非ゼロの並びで終わるデータは、末尾に 0x01 を付けない（一般的な実装と同じ）。
"""

from __future__ import annotations

__all__ = ["CobsDecodeError", "decode", "encode"]

_MAX_BLOCK = 254


class CobsDecodeError(ValueError):
    """COBS として不正なバイト列。"""


def encode(data: bytes) -> bytes:
    """data を COBS で符号化する。結果に 0x00 は含まれない。"""
    out = bytearray()
    block = bytearray()
    ended_with_full_block = False
    for byte in data:
        ended_with_full_block = False
        if byte == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
        else:
            block.append(byte)
            if len(block) == _MAX_BLOCK:
                out.append(_MAX_BLOCK + 1)
                out += block
                block.clear()
                ended_with_full_block = True
    if not ended_with_full_block:
        out.append(len(block) + 1)
        out += block
    return bytes(out)


def decode(data: bytes) -> bytes:
    """COBS を復号する。0x00 を含む・符号が範囲を超えるなどの不正は CobsDecodeError。"""
    if not data:
        raise CobsDecodeError("空のデータ")
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise CobsDecodeError(f"位置 {i} に 0x00 がある")
        i += 1
        end = i + code - 1
        if end > n:
            raise CobsDecodeError(f"位置 {i - 1} の符号 {code} がデータの末尾を超える")
        block = data[i:end]
        if 0 in block:
            raise CobsDecodeError("ブロックの中に 0x00 がある")
        out += block
        i = end
        if code <= _MAX_BLOCK and i < n:
            out.append(0)
    return bytes(out)
