"""CRC-16/CCITT-FALSE（多項式 0x1021、初期値 0xFFFF、反転なし、XOR 出力 0x0000）。

検査値："123456789"（ASCII）→ 0x29B1。
"""

from __future__ import annotations

from iotspec.constants import CRC16_INIT, CRC16_POLY, CRC16_XOROUT

__all__ = ["crc16_ccitt_false"]


def crc16_ccitt_false(data: bytes, init: int = CRC16_INIT) -> int:
    """data の CRC-16/CCITT-FALSE を返す。"""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ CRC16_POLY) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc ^ CRC16_XOROUT
