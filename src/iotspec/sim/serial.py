"""仮想シリアル（v0.2.1 #2：2つのモード）。

- own_pty：pty.openpty() で疑似端末を作る。相手（作品03・作品01のホスト版・gw-01 簡易版）は
  `peer_path` を開いてつなぐ。端末の行規律が 0x00 や改行を書き換えないよう raw モードにする
- device：pyserial で指定したデバイスパスを開く（Renode や作品03の本体が作ったポートにつなぐ用）

どちらも非ブロッキングで、asyncio のイベントループに読み込みを登録して使う。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import termios
import tty
from collections.abc import Callable
from typing import Protocol

import serial

from iotspec.constants import UART_BAUD_RATE

__all__ = ["DevicePort", "OwnPty", "SerialLink", "open_link"]

_READ_CHUNK = 4096


class SerialLink(Protocol):
    """バイト列を読み書きできる仮想シリアルの片側。"""

    def fileno(self) -> int: ...

    def write(self, data: bytes) -> None: ...

    def read_available(self) -> bytes: ...

    def close(self) -> None: ...


class OwnPty:
    """自分で疑似端末を作る。自分は master 側を使い、相手は peer_path（slave 側）を開く。"""

    def __init__(self) -> None:
        self._master, self._slave = pty.openpty()
        tty.setraw(self._slave, termios.TCSANOW)
        os.set_blocking(self._master, False)
        self.peer_path: str = os.ttyname(self._slave)
        """相手が開くパス（例：/dev/ttys012、/dev/pts/3）。"""

    def fileno(self) -> int:
        return self._master

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(self._master, view)
            except BlockingIOError:
                continue
            view = view[written:]

    def read_available(self) -> bytes:
        try:
            return os.read(self._master, _READ_CHUNK)
        except (BlockingIOError, OSError):
            return b""

    def close(self) -> None:
        for fd in (self._master, self._slave):
            with contextlib.suppress(OSError):
                os.close(fd)


class DevicePort:
    """pyserial で既存のシリアルポート（またはほかの代役の pty）を開く。"""

    def __init__(self, path: str, baudrate: int = UART_BAUD_RATE) -> None:
        self.path = path
        self._port = serial.Serial(path, baudrate=baudrate, timeout=0, write_timeout=1)

    def fileno(self) -> int:
        return int(self._port.fileno())

    def write(self, data: bytes) -> None:
        # flush()（tcdrain）は macOS の pty で戻らないことがあるので呼ばない
        self._port.write(data)

    def read_available(self) -> bytes:
        try:
            waiting = self._port.in_waiting
        except OSError:
            return b""
        return bytes(self._port.read(waiting or 1))

    def close(self) -> None:
        self._port.close()


def open_link(path: str | None) -> SerialLink:
    """path が None なら own_pty、指定があれば device モードで開く。"""
    return OwnPty() if path is None else DevicePort(path)


def add_reader(link: SerialLink, on_data: Callable[[bytes], None]) -> Callable[[], None]:
    """イベントループに読み込みを登録し、解除する関数を返す。"""
    loop = asyncio.get_running_loop()
    fd = link.fileno()

    def ready() -> None:
        data = link.read_available()
        if data:
            on_data(data)

    loop.add_reader(fd, ready)

    def remove() -> None:
        loop.remove_reader(fd)

    return remove
