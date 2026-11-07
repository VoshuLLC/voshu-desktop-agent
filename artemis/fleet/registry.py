"""Fleet device registry — persisted state for every managed endpoint.

Kinds: "avd" (cloned emulator), "physical" (usb/network phone), "remote"
(tailnet adb endpoint — future redroid hosts). AVD clones register their
emulator-<port> serial once booted; the registry survives daemon restarts.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

FLEET_DIR = Path(os.environ.get("ARTEMIS_FLEET_DIR", "traces/fleet"))
REGISTRY_FILE = FLEET_DIR / "devices.json"


@dataclass
class FleetDevice:
    id: str                      # stable id, e.g. "emu-01", "pixel-6"
    name: str                    # display name
    serial: str                  # adb serial: emulator-5556 / host:port / usb id
    kind: str                    # avd | physical | remote
    status: str = "booting"      # booting | free | busy | draining | dead | offline
    avd_name: str | None = None  # AVD name when kind=avd
    golden_rev: str | None = None
    task: str | None = None      # current goal text when busy
    session_id: str | None = None
    booted_at: float = 0.0
    last_seen: float = 0.0
    meta: dict = field(default_factory=dict)


class FleetRegistry:
    def __init__(self, path: Path = REGISTRY_FILE):
        self.path = path
        self._lock = asyncio.Lock()
        self._devices: dict[str, FleetDevice] = {}
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            for d in data.get("devices", []):
                dev = FleetDevice(**{k: v for k, v in d.items() if k in FleetDevice.__dataclass_fields__})
                if dev.status in ("busy", "booting"):  # stale state on restart
                    dev.status = "offline"
                    dev.task = None
                self._devices[dev.id] = dev
        except Exception:
            self._devices = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"devices": [asdict(d) for d in self._devices.values()]}, indent=1))
        tmp.replace(self.path)

    def list(self) -> list[FleetDevice]:
        return sorted(self._devices.values(), key=lambda d: d.id)

    def get(self, device_id: str) -> FleetDevice | None:
        return self._devices.get(device_id)

    def by_serial(self, serial: str) -> FleetDevice | None:
        return next((d for d in self._devices.values() if d.serial == serial), None)

    async def upsert(self, dev: FleetDevice):
        async with self._lock:
            self._devices[dev.id] = dev
            self._save()

    async def update(self, device_id: str, **fields):
        async with self._lock:
            if d := self._devices.get(device_id):
                for k, v in fields.items():
                    if hasattr(d, k):
                        setattr(d, k, v)
                d.last_seen = time.time()
                self._save()

    async def remove(self, device_id: str):
        async with self._lock:
            self._devices.pop(device_id, None)
            self._save()


fleet_registry = FleetRegistry()
