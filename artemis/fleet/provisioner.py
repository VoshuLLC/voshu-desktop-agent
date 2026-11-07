"""Fleet provisioner — golden AVD lifecycle and clone management.

Layout (all on the DevData external drive per workspace drive rules):
    /Volumes/DevData/Android/avd/voshu-farm/
        voshu-gold.avd / voshu-gold.ini   # the baseline — never booted dirty
        voshu-emu-01.avd / .ini           # clones, COW-free full copies
        GOLDEN_REV                        # bump to re-baseline the fleet

Clone = copy .avd dir + fresh .ini. Emulator ports are even, 5554+;
serial for Artemis is emulator-<port>. First boot regenerates userdata
from the golden snapshot when fastboot is enabled.
"""

import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from artemis.toolchain import find_adb
from artemis.fleet.registry import FleetDevice, fleet_registry

SDK = Path(os.environ.get("ANDROID_SDK_ROOT", Path.home() / "Library/Android/sdk"))
FARM_HOME = Path(os.environ.get("ARTEMIS_FARM_HOME", "/Volumes/DevData/Android/avd/voshu-farm"))
GOLDEN_AVD = os.environ.get("ARTEMIS_GOLDEN_AVD", "voshu-gold")
SYSTEM_IMAGE = os.environ.get("ARTEMIS_FLEET_IMAGE", "system-images;android-34;google_apis;arm64-v8a")
DEVICE_DEF = os.environ.get("ARTEMIS_FLEET_DEVICE", "pixel_6")
FIRST_PORT, LAST_PORT = 5554, 5598  # emulator ports are even; serials emulator-<port>

EMULATOR = SDK / "emulator" / "emulator"
AVDMANAGER = SDK / "cmdline-tools" / "latest" / "bin" / "avdmanager"


def _env() -> dict:
    env = dict(os.environ)
    env["ANDROID_AVD_HOME"] = str(FARM_HOME)
    env["ANDROID_SDK_ROOT"] = str(SDK)
    return env


async def _run(*args, timeout=60) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def adb_devices() -> list[tuple[str, str]]:
    """[(serial, state)] live from adb."""
    rc, out, _ = await _run(find_adb(), "devices")
    return [
        (p[0], p[1])
        for p in (ln.split() for ln in out.splitlines()[1:] if ln.strip())
        if len(p) >= 2
    ]


def golden_rev() -> str | None:
    f = FARM_HOME / "GOLDEN_REV"
    return f.read_text().strip() if f.exists() else None


async def ensure_golden() -> dict:
    """Verify the golden AVD exists; create it when missing (first run)."""
    avd_dir = FARM_HOME / f"{GOLDEN_AVD}.avd"
    ini = FARM_HOME / f"{GOLDEN_AVD}.ini"
    if avd_dir.exists() and ini.exists():
        return {"exists": True, "avd": GOLDEN_AVD, "rev": golden_rev()}
    FARM_HOME.mkdir(parents=True, exist_ok=True)
    rc, out, err = await _run(
        str(AVDMANAGER), "create", "avd", "-n", GOLDEN_AVD, "-k", SYSTEM_IMAGE,
        "-d", DEVICE_DEF, "-c", "2048M", "--force",
        timeout=120,
    )
    if rc != 0:
        return {"exists": False, "error": err.strip() or out.strip()}
    (FARM_HOME / "GOLDEN_REV").write_text(time.strftime("rev-%Y%m%d"))
    return {"exists": True, "created": True, "avd": GOLDEN_AVD, "rev": golden_rev()}


def _clone_avd(src_name: str, dst_name: str) -> Path:
    src_dir, dst_dir = FARM_HOME / f"{src_name}.avd", FARM_HOME / f"{dst_name}.avd"
    shutil.copytree(src_dir, dst_dir, symlinks=True)
    (FARM_HOME / f"{dst_name}.ini").write_text(
        f"avd.ini.encoding=UTF-8\npath={dst_dir}\npath.rel=avd/{dst_name}.avd\ntarget=android-34\n"
    )
    return dst_dir


async def _wait_boot(serial: str, timeout_s: int = 180) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc, out, _ = await _run(
            find_adb(), "-s", serial, "shell", "getprop", "sys.boot_completed", timeout=10
        )
        if rc == 0 and out.strip() == "1":
            return True
        await asyncio.sleep(3)
    return False


def _free_port() -> int | None:
    used = set()
    for ini in FARM_HOME.glob("voshu-emu-*.json"):
        try:
            used.add(int(__import__("json").loads(ini.read_text()).get("port", 0)))
        except Exception:
            pass
    for dev in fleet_registry.list():
        m = re.match(r"emulator-(\d+)", dev.serial or "")
        if m:
            used.add(int(m.group(1)))
    for p in range(FIRST_PORT, LAST_PORT + 1, 2):
        if p not in used:
            return p
    return None


def _next_emu_id() -> str:
    ids = {
        int(m.group(1))
        for d in fleet_registry.list()
        if (m := re.match(r"voshu-emu-(\d+)", d.id))
    }
    n = 1
    while n in ids:
        n += 1
    return f"voshu-emu-{n:02d}"


async def provision(count: int = 1) -> list[dict]:
    """Clone the golden AVD `count` times, boot each headless, register."""
    gold = await ensure_golden()
    if not gold.get("exists"):
        return [{"ok": False, "error": f"golden unavailable: {gold.get('error')}"}]
    results = []
    for _ in range(count):
        port = _free_port()
        if port is None:
            results.append({"ok": False, "error": "no free emulator ports"})
            break
        dev_id = _next_emu_id()
        serial = f"emulator-{port}"
        dev = FleetDevice(
            id=dev_id, name=dev_id.replace("-", " ").title(), serial=serial,
            kind="avd", status="booting", avd_name=dev_id, golden_rev=gold.get("rev"),
            booted_at=time.time(), meta={"port": port},
        )
        await fleet_registry.upsert(dev)
        try:
            _clone_avd(GOLDEN_AVD, dev_id)
            log = open(FARM_HOME / f"{dev_id}.boot.log", "w")
            proc = await asyncio.create_subprocess_exec(
                str(EMULATOR), "-avd", dev_id, "-port", str(port),
                "-no-window", "-gpu", "swiftshader_indirect", "-no-snapshot-save",
                "-read-only" if False else "-no-boot-anim",
                env=_env(), stdout=log, stderr=log,
            )
            dev.meta["pid"] = proc.pid
            await fleet_registry.upsert(dev)
            if await _wait_boot(serial):
                await fleet_registry.update(dev_id, status="free")
                results.append({"ok": True, "id": dev_id, "serial": serial})
            else:
                await fleet_registry.update(dev_id, status="dead")
                results.append({"ok": False, "id": dev_id, "error": "boot timeout"})
        except Exception as e:
            await fleet_registry.update(dev_id, status="dead")
            results.append({"ok": False, "id": dev_id, "error": str(e)})
    return results


async def destroy(device_id: str) -> dict:
    """Kill the emulator and delete its clone. Golden is never destroyed."""
    dev = fleet_registry.get(device_id)
    if not dev:
        return {"ok": False, "error": "unknown device"}
    if dev.id == GOLDEN_AVD:
        return {"ok": False, "error": "golden image is protected"}
    if dev.serial:
        await _run(find_adb(), "-s", dev.serial, "emu", "kill", timeout=10)
    if pid := dev.meta.get("pid"):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    for f in (FARM_HOME / f"{dev_id}.ini", FARM_HOME / f"{dev_id}.boot.log"):
        f.unlink(missing_ok=True)
    shutil.rmtree(FARM_HOME / f"{dev_id}.avd", ignore_errors=True)
    await fleet_registry.remove(device_id)
    return {"ok": True, "id": device_id}


async def reboot(device_id: str) -> dict:
    dev = fleet_registry.get(device_id)
    if not dev or dev.kind != "avd":
        return {"ok": False, "error": "not a managed avd"}
    await fleet_registry.update(device_id, status="booting")
    await _run(find_adb(), "-s", dev.serial, "emu", "kill", timeout=10)
    await asyncio.sleep(2)
    port = int(dev.meta.get("port", FIRST_PORT))
    log = open(FARM_HOME / f"{dev_id}.boot.log", "a")
    proc = await asyncio.create_subprocess_exec(
        str(EMULATOR), "-avd", dev.avd_name or dev_id, "-port", str(port),
        "-no-window", "-gpu", "swiftshader_indirect", "-no-boot-anim",
        env=_env(), stdout=log, stderr=log,
    )
    dev.meta["pid"] = proc.pid
    await fleet_registry.upsert(dev)
    ok = await _wait_boot(dev.serial)
    await fleet_registry.update(device_id, status="free" if ok else "dead")
    return {"ok": ok, "id": device_id}


async def sync_adb() -> list[FleetDevice]:
    """Merge live `adb devices` into the registry (physical + remote hosts)."""
    live = dict(await adb_devices())
    for dev in fleet_registry.list():
        if dev.serial in live:
            state = live[dev.serial]
            if state == "device" and dev.status in ("dead", "offline", "booting"):
                await fleet_registry.update(dev.id, status="free" if not dev.task else "busy")
            elif state != "device" and dev.status not in ("booting",):
                await fleet_registry.update(dev.id, status="dead")
        elif dev.kind != "avd":
            await fleet_registry.update(dev.id, status="offline")
    known_serials = {d.serial for d in fleet_registry.list()}
    for serial, state in live.items():
        if serial in known_serials or state != "device":
            continue
        kind = "physical" if ":" not in serial and not serial.startswith("emulator-") else "remote"
        dev = FleetDevice(
            id=re.sub(r"[^a-zA-Z0-9]+", "-", serial).strip("-").lower()[:24],
            name=serial, serial=serial, kind=kind, status="free", last_seen=time.time(),
        )
        await fleet_registry.upsert(dev)
    return fleet_registry.list()
