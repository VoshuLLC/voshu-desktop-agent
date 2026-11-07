"""Per-device live screen streaming — one capture loop per serial.

Same technique as apps.admin_console device_stream_service (adb exec-out
screencap -> multipart MJPEG) but keyed by serial so every farm tile gets
its own loop, refcounted per listener and stopped when nobody watches.
"""

import asyncio
import logging
import subprocess
import time
from collections.abc import AsyncGenerator

from artemis.toolchain import find_adb

logger = logging.getLogger("artemis.fleet.stream")


class _DeviceLoop:
    def __init__(self, serial: str):
        self.serial = serial
        self.listeners = 0
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self.task: asyncio.Task | None = None


class FleetStreamService:
    """Manages concurrent per-device screen capture loops."""

    def __init__(self):
        self._loops: dict[str, _DeviceLoop] = {}
        self._lock = asyncio.Lock()

    async def _capture(self, loop: _DeviceLoop):
        adb = find_adb()
        logger.info(f"[FleetStream] capture start {loop.serial}")
        while loop.listeners > 0:
            try:
                start_t = time.time()
                proc = await asyncio.create_subprocess_exec(
                    adb, "-s", loop.serial, "exec-out", "screencap", "-p",
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                out, _ = await proc.communicate()
                if proc.returncode == 0 and len(out) > 1000:
                    loop.frame, loop.frame_at = out, time.time()
                await asyncio.sleep(max(0.05, 0.15 - (time.time() - start_t)))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[FleetStream] {loop.serial}: {e}")
                await asyncio.sleep(0.6)
        logger.info(f"[FleetStream] capture stop {loop.serial}")

    async def attach(self, serial: str) -> _DeviceLoop:
        async with self._lock:
            loop = self._loops.setdefault(serial, _DeviceLoop(serial))
            loop.listeners += 1
            if loop.task is None or loop.task.done():
                loop.task = asyncio.create_task(self._capture(loop))
            return loop

    async def detach(self, serial: str):
        async with self._lock:
            loop = self._loops.get(serial)
            if not loop:
                return
            loop.listeners = max(0, loop.listeners - 1)
            if loop.listeners == 0 and loop.task and not loop.task.done():
                loop.task.cancel()

    async def frame(self, serial: str) -> bytes | None:
        loop = await self.attach(serial)
        try:
            deadline = time.time() + 3.0
            while loop.frame is None and time.time() < deadline:
                await asyncio.sleep(0.08)
            return loop.frame
        finally:
            await self.detach(serial)

    async def mjpeg(self, serial: str) -> AsyncGenerator[bytes, None]:
        loop = await self.attach(serial)
        try:
            last = 0.0
            while True:
                if loop.frame and loop.frame_at > last:
                    last = loop.frame_at
                    yield (b"--frame\r\nContent-Type: image/png\r\nContent-Length: "
                           + str(len(loop.frame)).encode() + b"\r\n\r\n" + loop.frame + b"\r\n")
                await asyncio.sleep(0.05)
        finally:
            await self.detach(serial)


fleet_stream_service = FleetStreamService()
