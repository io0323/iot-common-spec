"""docker compose の sim サービスで normal_day を流す。

受信した全電文が Schema に適合することを確かめる。

前提：conftest.py の前提に加えて docker compose が使えること（なければ skip）。
シミュレータのイメージを作るので初回は時間がかかる。normal_day を60倍速で仮想5分（実時間5秒）流す。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from iotspec.constants import DEVICE_MCU_CPP_01
from iotspec.schema import validate
from iotspec.sim.mqtt import BrokerAddress

from .conftest import observing

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
VIRTUAL_SECONDS = 300
SPEED = 60
BUILD_TIMEOUT_S = 600


def _compose(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


async def test_compose_sim_normal_day_conforms(broker: BrokerAddress) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker がない")
    build = await asyncio.to_thread(_compose, "build", "sim", timeout=BUILD_TIMEOUT_S)
    assert build.returncode == 0, build.stderr[-2000:]
    async with observing(broker, f"iot/v1/+/{DEVICE_MCU_CPP_01}/#") as observer:
        run = await asyncio.to_thread(
            _compose,
            "run",
            "--rm",
            "sim",
            "run",
            "normal_day",
            "--speed",
            str(SPEED),
            "--duration-s",
            str(VIRTUAL_SECONDS),
            timeout=BUILD_TIMEOUT_S,
        )
        assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    messages = [m for _, m in observer.messages if m.get("boot_id")]
    boot_ids = {m["boot_id"] for m in messages if m["payload"].get("status") != "offline"}
    ours = [m for m in messages if m["boot_id"] in boot_ids]
    assert len(ours) >= VIRTUAL_SECONDS * 0.9
    assert [validate(m) for m in ours if validate(m)] == []
    seqs: dict[str, list[int]] = defaultdict(list)
    for m in ours:
        seqs[m["schema"]].append(m["seq"])
    for schema, values in seqs.items():
        assert sorted(values) == values, schema
