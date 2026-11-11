# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HTTP device driver for agentd-hosted desktops (VOSHU-DEVICE-CONTROL §A.5).

Drives a remote `macagentd`-style desktop agent over HTTP so a fleet
"mac"|"win" device can run the same task pipeline as an adb device:

    task -> HttpDesktopDriver -> POST {host}/input|/key|/shell|/type|/app
             -> GET  {host}/screenshot|/frame|/health|/hierarchy

Coordinates are in the desktop's point space, which /health reports as
screen {w, h, unit:"points"} — the driver keeps that as its pixel space
so absolute taps map 1:1.
"""

import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import httpx

from artemis.drivers.base import (
    BaseDeviceDriver,
    KeyCode,
    ScreenData,
    SwipeDirection,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
_LONG_TIMEOUT = httpx.Timeout(120.0, connect=8.0)


class HttpDesktopDriver(BaseDeviceDriver):
    """BaseDeviceDriver over an agentd HTTP surface (kind mac|win)."""

    def __init__(
        self,
        device_id: str,
        base_url: str,
        token: str | None = None,
        width: int = 1440,
        height: int = 900,
        platform: str = "mac",
    ) -> None:
        if not base_url:
            raise ValueError("HttpDesktopDriver needs an agentd base_url")
        self._id = device_id
        self._base = base_url.rstrip("/")
        self._token = token
        self._w = int(width)
        self._h = int(height)
        self._platform = platform
        self._client: httpx.AsyncClient | None = None

    @property
    def device_id(self) -> str:
        return self._id

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._w, self._h)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _cli(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base, headers=self._headers(), timeout=_TIMEOUT
            )
        return self._client

    async def _post(self, path: str, body: dict, *, long: bool = False) -> dict:
        cli = await self._cli()
        r = await cli.request(
            "POST", path, json=body, timeout=_LONG_TIMEOUT if long else _TIMEOUT
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400 or data.get("ok") is False:
            raise RuntimeError(
                f"agentd {path} -> {r.status_code}: "
                f"{data.get('error') or r.text[:200]}"
            )
        return data

    async def _get_json(self, path: str) -> dict:
        cli = await self._cli()
        r = await cli.get(path)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

    async def connect(self) -> None:
        health = await self._get_json("/health")
        if not health.get("ok"):
            raise RuntimeError(f"agentd /health not ok: {health.get('error')}")
        scr = health.get("screen") or {}
        if scr.get("w") and scr.get("h"):
            self._w, self._h = int(scr["w"]), int(scr["h"])

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_screen_data(self, skip_settling: bool = False) -> ScreenData:
        cli = await self._cli()
        r = await cli.get("/screenshot", params={"format": "png"})
        if r.status_code != 200:
            raise RuntimeError(f"agentd /screenshot -> {r.status_code}")
        shot = r.content
        # Desktops have no DOM; the agentd AX hierarchy is the element source.
        elements: list[dict[str, Any]] = []
        try:
            hier = await self._get_json("/hierarchy")
            if isinstance(hier.get("elements"), list):
                elements = hier["elements"]
        except Exception:
            pass  # AX may be ungranted — screenshot still valid, stay honest.
        return ScreenData(
            screenshot_bytes=shot,
            screenshot_base64=base64.b64encode(shot).decode("ascii"),
            ui_hierarchy_xml=None,
            ui_elements=elements,
            width=self._w,
            height=self._h,
            timestamp=time.time(),
            platform=self._platform,
        )

    # -- input -----------------------------------------------------------
    async def tap(
        self,
        x: int,
        y: int,
        duration_ms: int = 100,
        times: int = 1,
        delay_ms: int = 100,
    ) -> bool:
        for i in range(max(1, times)):
            await self._post("/input", {"action": "tap", "x": x, "y": y})
            if i < times - 1:
                await asyncio.sleep(max(0.0, delay_ms / 1000.0))
        return True

    async def long_press(self, x: int, y: int, duration_ms: int = 1000) -> bool:
        await self._post(
            "/input", {"action": "long", "x": x, "y": y, "duration_ms": duration_ms}
        )
        return True

    async def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 800,
    ) -> bool:
        await self._post(
            "/input",
            {
                "action": "swipe",
                "x": start_x, "y": start_y,
                "x2": end_x, "y2": end_y,
                "duration_ms": duration_ms,
            },
        )
        return True

    async def swipe_direction(
        self,
        direction: SwipeDirection | str,
        duration_ms: int = 800,
    ) -> bool:
        cx, cy = self._w // 2, self._h // 2
        off_x, off_y = self._w // 4, self._h // 4
        d = direction.value if isinstance(direction, SwipeDirection) else str(direction).lower()
        sx, sy, ex, ey = {
            "up": (cx, cy + off_y, cx, cy - off_y),
            "down": (cx, cy - off_y, cx, cy + off_y),
            "left": (cx + off_x, cy, cx - off_x, cy),
            "right": (cx - off_x, cy, cx + off_x, cy),
        }.get(d, (cx, cy + off_y, cx, cy - off_y))
        return await self.swipe(sx, sy, ex, ey, duration_ms)

    async def input_text(self, text: str, clear_existing: bool = True) -> bool:
        # agentd's /type performs select-all + delete itself when asked.
        await self._post(
            "/type", {"text": text, "clear_existing": clear_existing},
            long=len(text) > 200,
        )
        return True

    # Android KeyCode -> macagentd key name. HOME has no desktop analog
    # (agentd's own "HOME" triggers Spotlight) so it degrades to ESC.
    _KEY_XLATE = {
        "home": "ESC",
        "back": "ESC",
        "enter": "ENTER",
        "delete": "BACKSPACE",
        "power": "POWER",  # agentd answers an honest 501 for this
        "app_switch": "APP_SWITCH",
        "volume_up": "VOLUME_UP",
        "volume_down": "VOLUME_DOWN",
    }

    async def press_key(self, key: KeyCode | str | int) -> bool:
        name = key.value if isinstance(key, KeyCode) else str(key)
        name = self._KEY_XLATE.get(name.lower(), name.upper())
        await self._post("/key", {"key": name})
        return True

    async def launch_app(self, package_name: str) -> bool:
        await self._post("/app", {"action": "launch", "name": package_name})
        return True

    async def stop_app(self, package_name: str) -> bool:
        await self._post("/app", {"action": "terminate", "name": package_name})
        return True

    async def get_current_package(self) -> str | None:
        # agentd does not report the focused app yet — stay honest.
        return None

    async def execute_shell(self, command: str, timeout_seconds: float = 15.0) -> str:
        res = await self._post(
            "/shell", {"cmd": command, "timeout_s": timeout_seconds}, long=True
        )
        out = str(res.get("out") or "")
        err = str(res.get("err") or "")
        return out + (f"\n[stderr] {err}" if err and res.get("rc") else "")

    async def start_video_recording(self, output_dir: Path | None = None) -> None:
        # agentd has no recording endpoint; MJPEG /stream exists but a
        # recorder is out of driver scope — report honestly instead of faking.
        logger.warning("video recording unsupported on desktop driver")

    async def stop_video_recording(self) -> str | None:
        return None
