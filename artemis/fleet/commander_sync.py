"""Federate Voshu Commander's paired computers into the Artemis fleet
registry for visibility.

Commander devices speak the VC-Pair command dialect (commands/poll/result),
NOT the agentd HTTP contract — so they register as kind "vcpair":
registry-visible (one orchestrator sees both rails) but not drivable by
HttpDesktopDriver until a VCPairDriver lands.

Configured via env — absent config means the lane is off, not fake:
    ARTEMIS_COMMANDER_URL   e.g. http://voshu-commander:8788
    ARTEMIS_COMMANDER_TOKEN optional bearer for authenticated endpoints
"""

import os
import time

import httpx

from artemis.fleet.registry import FleetDevice, fleet_registry
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

VCPAIR_KIND = "vcpair"


def _conf() -> tuple[str | None, dict]:
    base = (os.environ.get("ARTEMIS_COMMANDER_URL") or "").strip().rstrip("/")
    token = os.environ.get("ARTEMIS_COMMANDER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return (base or None), headers


async def sync_commander_devices() -> list[FleetDevice]:
    """Pull Commander's paired devices and mirror them into the registry.

    Truthful semantics: unreachable Commander -> existing vcpair rows go
    'offline' (the source rail can't be read, not that the boxes are down);
    removed remotely -> removed here too.
    """
    base, headers = _conf()
    if not base:
        return fleet_registry.list()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as cli:
            r = await cli.get(f"{base}/api/devices", headers=headers)
            devices = r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.warning(f"commander federation: unreachable ({type(e).__name__})")
        devices = None
    if not isinstance(devices, list):
        for d in fleet_registry.list():
            if d.kind == VCPAIR_KIND:
                await fleet_registry.update(d.id, status="offline")
        return fleet_registry.list()

    seen: set[str] = set()
    for c in devices:
        serial = str(c.get("serial") or c.get("deviceId") or c.get("id") or "").strip()
        if not serial:
            continue
        did = f"vcpair-{_slug(serial)}"
        seen.add(did)
        existing = fleet_registry.get(did)
        # Commander reports its own health; mirror it, don't recompute it.
        online = bool(c.get("online") or c.get("status") in ("online", "running"))
        status = "free" if online else "offline"
        meta = {
            "source": "commander",
            "commander_id": c.get("id") or c.get("deviceId"),
            "os": c.get("os") or c.get("platform"),
            "capabilities": c.get("capabilities") or c.get("tools") or [],
        }
        if existing:
            await fleet_registry.update(did, status=status, meta={**existing.meta, **meta})
        else:
            await fleet_registry.upsert(FleetDevice(
                id=did, name=str(c.get("name") or serial), serial=serial,
                kind=VCPAIR_KIND, host=None, status=status, meta=meta,
                last_seen=time.time() if online else 0.0,
            ))
    # rows Commander no longer reports are gone, not merely offline
    for d in fleet_registry.list():
        if d.kind == VCPAIR_KIND and d.id not in seen:
            await fleet_registry.remove(d.id)
    return fleet_registry.list()


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()[:24] or "device"
