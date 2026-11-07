"""Hosted-device transport — kind "mac" | "win" agentd endpoints.

Phones are spawned and driven over adb; desktops are not. An agent daemon
(artemis/desktop/macagentd.py, :7778) runs on the box and mirrors the action
dialect over HTTP. This module is the fleet-side half: enrollment (registry
insert + health check), liveness sync, and request forwarding for the fleet
router. Spec: voshu-docs VOSHU-DEVICE-CONTROL §A.4.

Token: per-device bearer stored in registry meta["token"] at enroll; falls
back to env MACAGENTD_TOKEN (the daemon's own env var).
"""

import os
import re
import time
from collections.abc import AsyncGenerator

import httpx

from artemis.fleet.registry import AGENT_KINDS, FleetDevice, fleet_registry

DEFAULT_AGENT_PORT = 7778
_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


def normalize_host(host: str, port: int | None = None) -> str:
    """'127.0.0.1' -> 'http://127.0.0.1:7778'; 'http://h:9' stays."""
    host = host.strip().rstrip("/")
    if not host:
        raise ValueError("host required")
    if not re.match(r"^https?://", host):
        host = f"http://{host}"
    if not re.search(r":\d+$", urlparse_host(host)):
        host = f"{host}:{port or DEFAULT_AGENT_PORT}"
    return host


def urlparse_host(url: str) -> str:
    return url.split("://", 1)[1]


def agentd_base(dev: FleetDevice) -> str | None:
    host = (dev.host or "").rstrip("/")
    return host or None


def _headers(dev: FleetDevice) -> dict[str, str]:
    token = dev.meta.get("token") or os.environ.get("MACAGENTD_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def agentd_json(dev: FleetDevice, method: str, path: str,
                      *, json: dict | None = None, timeout: float = 10.0) -> dict:
    """One JSON call against the device agentd; transport errors stay honest."""
    base = agentd_base(dev)
    if not base:
        return {"ok": False, "error": "device has no agentd host"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.request(method, base + path, json=json,
                                     headers=_headers(dev))
        if r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
            if isinstance(body, dict) and r.status_code >= 400 and "error" not in body:
                body["error"] = f"agentd HTTP {r.status_code}"
            return body
        return {"ok": r.status_code < 400, "status": r.status_code,
                "error": r.text[:300] if r.status_code >= 400 else None}
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        return {"ok": False, "error": f"agentd unreachable: {type(e).__name__}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def agentd_frame(dev: FleetDevice) -> tuple[bytes | None, str]:
    """Raw image bytes + media type from the agentd, else (None, error)."""
    base = agentd_base(dev)
    if not base:
        return None, "device has no agentd host"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(base + "/frame", headers=_headers(dev))
        if r.status_code == 200:
            return r.content, r.headers.get("content-type", "image/png")
        return None, f"agentd HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return None, f"agentd unreachable: {type(e).__name__}"


async def agentd_stream(dev: FleetDevice) -> AsyncGenerator[bytes, None]:
    """Pass through the agentd MJPEG stream unchanged."""
    base = agentd_base(dev)
    if not base:
        return
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", base + "/stream",
                                     headers=_headers(dev)) as r:
                if r.status_code != 200:
                    return
                async for chunk in r.aiter_raw():
                    yield chunk
    except Exception:
        return


async def agentd_health(dev: FleetDevice) -> dict:
    base = agentd_base(dev)
    if not base:
        return {"ok": False, "error": "no host"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0)) as client:
            r = await client.get(base + "/health", headers=_headers(dev))
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


async def sync_agents() -> list[FleetDevice]:
    """Health-poll every agent kind (mac|win) — the sync_adb analog."""
    for dev in fleet_registry.list():
        if dev.kind not in AGENT_KINDS or not dev.host:
            continue
        health = await agentd_health(dev)
        if health.get("ok"):
            if dev.status in ("dead", "offline", "booting"):
                await fleet_registry.update(
                    dev.id, status="busy" if dev.task else "free")
            else:
                await fleet_registry.update(dev.id)
        elif dev.status not in ("booting",):
            await fleet_registry.update(dev.id, status="offline")
    return fleet_registry.list()


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()[:24] or "device"


async def enroll(*, kind: str, serial: str, host: str,
                 port: int | None = None, name: str | None = None,
                 token: str | None = None,
                 device_id: str | None = None) -> dict:
    """Register an externally-hosted agentd device — no spawn, just insert.

    `serial` stays the addressing key; `host` is the agentd base URL.
    A health check runs immediately; an unreachable agent still enrolls
    (status "offline") so boxes can join before their daemon is up.
    """
    if kind not in AGENT_KINDS:
        return {"ok": False, "error": f"kind must be one of {AGENT_KINDS}"}
    try:
        base = normalize_host(host, port)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    dev_id = device_id or _slug(serial)
    meta = {"enrolled": True, "agent_port": port or DEFAULT_AGENT_PORT}
    if token:
        meta["token"] = token
    dev = FleetDevice(
        id=dev_id, name=name or serial, serial=serial, kind=kind,
        host=base, status="booting", meta=meta, last_seen=time.time(),
    )
    await fleet_registry.upsert(dev)
    health = await agentd_health(dev)
    status = "free" if health.get("ok") else "offline"
    await fleet_registry.update(dev_id, status=status)
    return {"ok": True, "id": dev_id, "serial": serial, "kind": kind,
            "host": base, "status": status,
            "health": health if health.get("ok") else
                      {"ok": False, "error": health.get("error")}}
