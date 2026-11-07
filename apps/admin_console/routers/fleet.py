"""Fleet API — Voshu device-farm surface for the admin console.

GET  /api/fleet/devices            live registry + adb merge + agentd health
GET  /api/fleet/devices/{id}/stream  per-device MJPEG
GET  /api/fleet/devices/{id}/frame   single PNG frame
POST /api/fleet/provision          {count} -> clone golden + boot
POST /api/fleet/enroll             {kind,serial,host,port?,name?,token?} ->
                                   register an external agentd device (mac|win)
POST /api/fleet/devices/{id}/run   {goal} -> task on that device serial
POST /api/fleet/devices/{id}/input {x,y} | {x1,y1,x2,y2,duration} -> tap/swipe
POST /api/fleet/devices/{id}/key   {key} -> BACK/HOME/APP_SWITCH/ENTER
POST /api/fleet/devices/{id}/shell {cmd} -> adb shell / agentd shell
POST /api/fleet/devices/{id}/reboot
GET  /api/fleet/devices/{id}/memory  durable workspace pack (notes + last runs)
DELETE /api/fleet/devices/{id}     kill + delete clone (unenroll for mac|win)
GET  /api/fleet/status             counts, golden rev, ports

kind "mac"|"win" devices carry a `host` (agentd base URL) and forward
input/key/shell/frame/stream to it instead of adb — same request/response
shapes. Provision/reboot/run are AVD- or adb-bound and answer 501 honestly.
"""

import asyncio
import subprocess
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from artemis.toolchain import find_adb
from artemis.recipes.workspaces import memory_pack
from artemis.fleet.agentd import (
    AGENT_KINDS, agentd_frame, agentd_json, agentd_stream, enroll, sync_agents,
)
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
    await sync_adb()
    devices = await sync_agents()  # health-poll kind mac|win agentd hosts
    return {
        "devices": [
            {
                "id": d.id, "name": d.name, "serial": d.serial, "kind": d.kind,
                "host": d.host,
                "status": d.status, "golden_rev": d.golden_rev, "task": d.task,
                "session_id": d.session_id, "booted_at": d.booted_at,
                "last_seen": d.last_seen,
            }
            for d in devices
        ],
        "golden": {"avd": GOLDEN_AVD, "rev": golden_rev()},
        "ts": time.time(),
    }


def _agentd_result(res: dict):
    """Map an agentd reply to the same client contract as the adb paths.

    Transport failures are honest 503s; semantic failures stay in-body
    ({ok:false,error}) exactly like adb's {ok:false,err}.
    """
    if "unreachable" in str(res.get("error", "")) or "no agentd host" in str(res.get("error", "")):
        raise HTTPException(503, res.get("error"))
    return res


@router.get("/api/fleet/devices/{device_id}/stream")
async def device_stream(device_id: str):
    d = _dev(device_id)
    if d.kind in AGENT_KINDS:
        return StreamingResponse(
            agentd_stream(d),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )
    return StreamingResponse(
        fleet_stream_service.mjpeg(d.serial),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/api/fleet/devices/{device_id}/frame")
async def device_frame(device_id: str):
    d = _dev(device_id)
    if d.kind in AGENT_KINDS:
        frame, err = await agentd_frame(d)
        if not frame:
            raise HTTPException(503, f"no frame — {err}")
        return Response(content=frame, media_type="image/png")
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


class EnrollRequest(BaseModel):
    kind: str = "mac"            # mac | win — agentd-hosted desktop kinds
    serial: str                  # addressing key, e.g. "mac-studio"
    host: str                    # agentd base: "127.0.0.1" | "http://h:7778"
    port: int | None = None
    name: str | None = None
    token: str | None = None     # per-device bearer, stored in registry meta


@router.post("/api/fleet/enroll")
async def enroll_device(req: EnrollRequest):
    """Register an externally-hosted agentd device — no spawn, no clone.

    Registry insert + immediate health check; an unreachable agent enrolls
    as "offline" so boxes can join before their daemon is up.
    """
    res = await enroll(
        kind=req.kind, serial=req.serial, host=req.host,
        port=req.port, name=req.name, token=req.token,
    )
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "enroll failed"))
    return res


class FleetRunRequest(BaseModel):
    goal: str
    profile: str | None = None


@router.post("/api/fleet/devices/{device_id}/run")
async def run_on_device(device_id: str, req: FleetRunRequest):
    d = _dev(device_id)
    if d.kind in AGENT_KINDS:
        raise HTTPException(
            501, f"task sessions for kind {d.kind} need the HttpDesktopDriver "
                 "(VOSHU-DEVICE-CONTROL §A.5) — input/key/shell/frame work today")
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
    # agentd extras (mac|win) — ignored by the adb path
    action: str | None = None  # click|double|right|long|swipe|scroll
    nx: float | None = None    # actuator dialect: normalized 0..1000
    ny: float | None = None
    nx2: float | None = None
    ny2: float | None = None
    dx: float | None = None    # scroll deltas (points; unit:"lines" for lines)
    dy: float | None = None
    unit: str | None = None
    times: int | None = None
    space: str | None = None   # explicit coord space for x/y: points|norm|norm1000


@router.post("/api/fleet/devices/{device_id}/input")
async def device_input(device_id: str, req: InputRequest):
    d = _dev(device_id)
    if d.kind in AGENT_KINDS:
        res = await agentd_json(
            d, "POST", "/input",
            json=req.model_dump(exclude_none=True), timeout=20.0)
        return _agentd_result(res)
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
    if d.kind in AGENT_KINDS:
        res = await agentd_json(d, "POST", "/key", json={"key": req.key})
        if res.get("ok") is False and "unknown key" in str(res.get("error", "")):
            raise HTTPException(400, res["error"])
        return _agentd_result(res)
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
    if d.kind in AGENT_KINDS:
        res = await agentd_json(d, "POST", "/shell", json={"cmd": cmd}, timeout=25.0)
        return _agentd_result(res)
    try:
        rc, out, err = await _adb(d.serial, "shell", cmd, timeout=20)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    return {"ok": rc == 0, "out": out.strip()[:4000], "err": err.strip()[:2000]}


@router.get("/api/fleet/devices/{device_id}/memory")
async def device_memory(device_id: str, last_n: int = 20):
    """Durable workspace pack for the planner: notes.md + last ``last_n``
    memory lines + remembered coords (``traces/workspaces/<serial>/``)."""
    d = _dev(device_id)
    return memory_pack(d.serial, last_n=last_n)


@router.post("/api/fleet/devices/{device_id}/reboot")
async def device_reboot(device_id: str):
    d = _dev(device_id)
    if d.kind in AGENT_KINDS:
        raise HTTPException(
            501, f"reboot is not supported for kind {d.kind} — the fleet does "
                 "not power-manage desktop hosts")
    return await reboot(device_id)


@router.delete("/api/fleet/devices/{device_id}")
async def device_delete(device_id: str):
    d = _dev(device_id)
    if d.kind in AGENT_KINDS:
        # honest unenroll: nothing was spawned, nothing is killed
        await fleet_registry.remove(device_id)
        return {"ok": True, "id": device_id, "unenrolled": True}
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
