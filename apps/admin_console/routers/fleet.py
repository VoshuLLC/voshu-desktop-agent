"""Fleet API — Voshu phone-farm surface for the admin console.

GET  /api/fleet/devices            live registry + adb merge
GET  /api/fleet/devices/{id}/stream  per-device MJPEG
GET  /api/fleet/devices/{id}/frame   single PNG frame
POST /api/fleet/provision          {count} -> clone golden + boot
POST /api/fleet/devices/{id}/run   {goal} -> task on that device serial
POST /api/fleet/devices/{id}/input {x,y} | {x1,y1,x2,y2,duration} -> tap/swipe
POST /api/fleet/devices/{id}/key   {key} -> BACK/HOME/APP_SWITCH/ENTER
POST /api/fleet/devices/{id}/shell {cmd} -> adb shell (read-only-ish ops)
POST /api/fleet/devices/{id}/reboot
DELETE /api/fleet/devices/{id}     kill + delete clone
GET  /api/fleet/status             counts, golden rev, ports
"""

import asyncio
import subprocess
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from artemis.toolchain import find_adb
from artemis.fleet.provisioner import (
    GOLDEN_AVD, FARM_HOME, adb_devices, destroy, ensure_golden, golden_rev,
    provision, reboot, sync_adb,
)
from artemis.fleet.registry import fleet_registry
from artemis.fleet.stream import fleet_stream_service

try:
    from admin_console.routers.tasks import RunRequest, run_task
except ImportError:
    from apps.admin_console.routers.tasks import RunRequest, run_task

router = APIRouter(tags=["fleet"])

_KEYCODES = {
    "BACK": 4, "HOME": 3, "APP_SWITCH": 187, "ENTER": 66,
    "POWER": 26, "VOLUME_UP": 24, "VOLUME_DOWN": 25,
}


def _dev(device_id: str):
    d = fleet_registry.get(device_id)
    if not d:
        raise HTTPException(404, f"unknown device {device_id}")
    return d


async def _adb(serial: str, *args, timeout=15) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        find_adb(), "-s", serial, *args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


@router.get("/api/fleet/devices")
async def list_devices():
    devices = await sync_adb()
    return {
        "devices": [
            {
                "id": d.id, "name": d.name, "serial": d.serial, "kind": d.kind,
                "status": d.status, "golden_rev": d.golden_rev, "task": d.task,
                "session_id": d.session_id, "booted_at": d.booted_at,
                "last_seen": d.last_seen,
            }
            for d in devices
        ],
        "golden": {"avd": GOLDEN_AVD, "rev": golden_rev()},
        "ts": time.time(),
    }


@router.get("/api/fleet/devices/{device_id}/stream")
async def device_stream(device_id: str):
    d = _dev(device_id)
    return StreamingResponse(
        fleet_stream_service.mjpeg(d.serial),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/api/fleet/devices/{device_id}/frame")
async def device_frame(device_id: str):
    d = _dev(device_id)
    frame = await fleet_stream_service.frame(d.serial)
    if not frame:
        raise HTTPException(503, "no frame yet — device may be booting")
    return Response(content=frame, media_type="image/png")


class ProvisionRequest(BaseModel):
    count: int = 1


@router.post("/api/fleet/provision")
async def provision_devices(req: ProvisionRequest):
    if req.count < 1 or req.count > 8:
        raise HTTPException(400, "count must be 1..8")
    gold = await ensure_golden()
    if not gold.get("exists"):
        return {"ok": False, "error": f"golden AVD unavailable: {gold.get('error')}", "created_golden": gold.get("created")}
    results = await provision(req.count)
    return {"ok": all(r.get("ok") for r in results), "results": results, "created_golden": gold.get("created", False)}


class FleetRunRequest(BaseModel):
    goal: str
    profile: str | None = None


@router.post("/api/fleet/devices/{device_id}/run")
async def run_on_device(device_id: str, req: FleetRunRequest):
    d = _dev(device_id)
    if d.status != "free":
        raise HTTPException(409, f"device is {d.status}")
    await fleet_registry.update(device_id, status="busy", task=req.goal[:160])
    rr = RunRequest(goal=req.goal, device_serial=d.serial)
    resp = await run_task(rr)
    sid = None
    try:
        sid = resp.get("tasks", [{}])[0].get("session_id")
    except Exception:
        pass
    await fleet_registry.update(device_id, session_id=sid)
    return resp


class InputRequest(BaseModel):
    x: float | None = None
    y: float | None = None
    x2: float | None = None
    y2: float | None = None
    duration_ms: int = 300
    # device-resolution coords; UI scales client->device itself
    device_w: int | None = None
    device_h: int | None = None
    cx: float | None = None  # client-normalized 0..1 coords (preferred)
    cy: float | None = None
    cx2: float | None = None
    cy2: float | None = None


@router.post("/api/fleet/devices/{device_id}/input")
async def device_input(device_id: str, req: InputRequest):
    d = _dev(device_id)
    if req.cx is not None and req.cy is not None:
        _, size, _ = await _adb(d.serial, "shell", "wm", "size")
        import re
        m = re.search(r"(\d+)x(\d+)", size)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        x, y = int(req.cx * w), int(req.cy * h)
        if req.cx2 is not None and req.cy2 is not None:
            x2, y2 = int(req.cx2 * w), int(req.cy2 * h)
            rc, out, err = await _adb(
                d.serial, "shell", "input", "swipe",
                str(x), str(y), str(x2), str(y2), str(req.duration_ms))
            return {"ok": rc == 0, "type": "swipe", "coords": [x, y, x2, y2], "err": err.strip()}
        rc, out, err = await _adb(d.serial, "shell", "input", "tap", str(x), str(y))
        return {"ok": rc == 0, "type": "tap", "coords": [x, y], "err": err.strip()}
    if req.x is not None and req.y is not None:
        if req.x2 is not None and req.y2 is not None:
            rc, out, err = await _adb(
                d.serial, "shell", "input", "swipe",
                str(int(req.x)), str(int(req.y)), str(int(req.x2)), str(int(req.y2)), str(req.duration_ms))
            return {"ok": rc == 0, "type": "swipe", "err": err.strip()}
        rc, out, err = await _adb(d.serial, "shell", "input", "tap", str(int(req.x)), str(int(req.y)))
        return {"ok": rc == 0, "type": "tap", "err": err.strip()}
    raise HTTPException(400, "need cx/cy (normalized) or x/y (device px)")


class KeyRequest(BaseModel):
    key: str


@router.post("/api/fleet/devices/{device_id}/key")
async def device_key(device_id: str, req: KeyRequest):
    d = _dev(device_id)
    code = _KEYCODES.get(req.key.upper())
    if code is None:
        raise HTTPException(400, f"unknown key; allowed: {sorted(_KEYCODES)}")
    rc, _, err = await _adb(d.serial, "shell", "input", "keyevent", str(code))
    return {"ok": rc == 0, "key": req.key.upper(), "err": err.strip()}


class ShellRequest(BaseModel):
    cmd: str


@router.post("/api/fleet/devices/{device_id}/shell")
async def device_shell(device_id: str, req: ShellRequest):
    d = _dev(device_id)
    cmd = req.cmd.strip()
    if not cmd or any(b in cmd for b in ("|", ";", "&", ">", "<", "`", "$(")):
        raise HTTPException(400, "single adb shell command, no shell metachars")
    try:
        rc, out, err = await _adb(d.serial, "shell", cmd, timeout=20)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    return {"ok": rc == 0, "out": out.strip()[:4000], "err": err.strip()[:2000]}


@router.post("/api/fleet/devices/{device_id}/reboot")
async def device_reboot(device_id: str):
    return await reboot(device_id)


@router.delete("/api/fleet/devices/{device_id}")
async def device_delete(device_id: str):
    return await destroy(device_id)


@router.get("/api/fleet/status")
async def fleet_status():
    devices = fleet_registry.list()
    live = await adb_devices()
    return {
        "total": len(devices),
        "by_status": {s: sum(1 for d in devices if d.status == s) for s in ("free", "busy", "booting", "dead", "offline")},
        "adb_live": len([s for _, s in live if s == "device"]),
        "golden": {"avd": GOLDEN_AVD, "rev": golden_rev(), "exists": (FARM_HOME / f"{GOLDEN_AVD}.avd").exists()},
        "farm_home": str(FARM_HOME),
    }
