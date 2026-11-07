"""Recipes API — record-once deterministic replay for the admin console.

GET    /api/recipes                  list saved recipes
POST   /api/recipes/from-session     {session_id, recipe_id, name?, tags?, param_hints?, app_package?} -> build + save
GET    /api/recipes/{id}             full recipe JSON
PUT    /api/recipes/{id}             replace recipe with a validated body (hand edits)
DELETE /api/recipes/{id}             remove recipe file
POST   /api/recipes/{id}/run         {device_serial?, variables?, profile?} -> queue a replay
GET    /api/recipes/runs/{run_id}    persisted RecipeRunResult / live run record

Recipe runs go through the same device machinery as goal tasks, but the queue
service's ``_build_worker_invocation`` accepts no per-task env override today,
so ``enqueue_recipe_run`` spawns the dedicated worker
``python -m artemis.recipes.worker`` itself and registers it exactly like a
queue-spawned run (queue ticket + ``state.active_runs`` + running queue item):
the unified queue, status, and stop paths all treat it as a normal session.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from artemis.config import WORKSPACE_ROOT
from artemis.recipes.models import Recipe, is_valid_recipe_id
from artemis.recipes.recorder import (
    RECIPE_RUNS_DIR,
    RecipeRecorder,
    delete_recipe,
    list_recipes,
    load_recipe,
)
from artemis.runtime import (
    AdbTarget,
    DeviceExecutionLock,
    clear_cancel_request,
    current_adb_endpoint,
    device_pool,
    trace_store,
)

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.services import worker_process_io
    from admin_console.services.task_queue_service import task_queue_service
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.services import worker_process_io
    from apps.admin_console.services.task_queue_service import task_queue_service

router = APIRouter(tags=["recipes"])

#: Live run bookkeeping keyed by run_id (== the run's session id).
_recipe_runs: dict[str, dict[str, Any]] = {}
#: Strong refs to in-flight run finalizers (asyncio keeps only weak refs).
_recipe_run_tasks: set[asyncio.Task] = set()


def _recipe_summary(recipe) -> dict[str, Any]:
    return {
        "id": recipe.id,
        "name": recipe.name,
        "tags": recipe.tags,
        "source_session": recipe.source_session,
        "created": recipe.created,
        "steps": len(recipe.steps),
        "skipped": len(recipe.skipped),
        "variables": recipe.variables,
    }


def _load_or_404(recipe_id: str):
    try:
        return load_recipe(recipe_id)
    except ValueError:
        raise HTTPException(400, f"invalid recipe id {recipe_id!r}") from None
    except FileNotFoundError:
        raise HTTPException(404, f"recipe not found: {recipe_id}") from None


# --- Recipe run dispatch -----------------------------------------------------


def _build_recipe_worker_invocation(
    recipe,
    run_id: str,
    device_serial: str | None,
    variables: dict[str, Any],
    target: AdbTarget,
) -> tuple[list[str], dict[str, str]]:
    """Worker cmdline + env, mirroring ``TaskQueueService._build_worker_invocation``.

    The recipe itself travels in the environment — ``ARTEMIS_RECIPE_ID`` and
    ``ARTEMIS_RECIPE_VARS`` — so the worker needs no LLM-facing arguments.
    """
    env = os.environ.copy()
    pythonpath_parts = [
        str(WORKSPACE_ROOT),
        str(WORKSPACE_ROOT / "apps" / "admin_console"),
        str(WORKSPACE_ROOT / "apps" / "cloud_service"),
        env.get("PYTHONPATH", ""),
    ]
    env["PYTHONPATH"] = os.pathsep.join([p for p in pythonpath_parts if p])
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if state.ipc_port is not None:
        env["ARTEMIS_IPC_PORT"] = str(state.ipc_port)
    env["ARTEMIS_SESSION_ID"] = run_id
    env["ARTEMIS_TASK_INGRESS"] = "recipe"
    env["ARTEMIS_TASK_WORKER"] = "1"
    env["ARTEMIS_RECIPE_ID"] = recipe.id
    env["ARTEMIS_RECIPE_VARS"] = json.dumps(variables)
    target.endpoint.apply_to_environment(env)
    env[DeviceExecutionLock.LOCK_SCOPE_ENV] = target.lock_scope
    if device_serial:
        env["ADB_DEVICE_SERIAL"] = device_serial

    cmd = [
        sys.executable,
        "-m",
        "artemis.recipes.worker",
        "--recipe",
        recipe.id,
        "--run-id",
        run_id,
    ]
    if device_serial:
        cmd.extend(["--device-serial", str(device_serial)])
    return cmd, env


def _read_run_result(run_id: str) -> dict[str, Any] | None:
    path = RECIPE_RUNS_DIR / f"{run_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _watch_recipe_run(
    run_id: str,
    proc: asyncio.subprocess.Process,
    queue_item: dict[str, Any],
    output_task: asyncio.Task | None,
) -> None:
    """Finalizer for one recipe worker — the recipe counterpart of
    ``TaskQueueService._execute_task_item``'s teardown."""
    returncode = -1
    try:
        returncode = await worker_process_io.wait_for_worker_process(proc)
    finally:
        await worker_process_io.finish_output_forwarder(output_task)

    manual_stop = run_id in state.manually_stopped_run_ids or run_id in (
        state.cancelled_session_ids
    )
    result = _read_run_result(run_id)
    if manual_stop:
        status = "cancelled"
    elif result is not None:
        status = "completed" if result.get("success") else "failed"
    else:
        status = "completed" if returncode == 0 else "failed"

    record = _recipe_runs.get(run_id)
    if record is not None:
        record.update(
            status=status,
            returncode=returncode,
            result=result,
            finished_at=time.time(),
        )

    # The worker normally ends its own session row; this is the fallback so a
    # crashed worker never leaves a 'running' session behind.
    try:
        if (session_repo.get_session_status(run_id) or "running") in (
            "running",
            "paused",
        ):
            session_repo.update_session_status(run_id, status, time.time())
    except Exception as exc:
        print(f"[recipe-runs] Could not persist terminal status for {run_id}: {exc}")

    task_queue_service._broadcast_event(
        "session_ended",
        {
            "session_id": run_id,
            "status": status,
            "was_stopped_manually": manual_stop,
        },
    )

    state.queue_items = [
        t
        for t in state.queue_items
        if not (isinstance(t, dict) and t.get("session_id") == run_id)
    ]
    state.active_runs.pop(run_id, None)
    state.cancelled_session_ids.discard(run_id)
    state.manually_stopped_run_ids.discard(run_id)
    try:
        clear_cancel_request(session_id=run_id, pid=getattr(proc, "pid", None))
    except (OSError, TypeError, ValueError):
        pass
    state.wake_event.set()


async def enqueue_recipe_run(
    recipe,
    device_serial: str | None = None,
    variables: dict[str, Any] | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Dispatches a recipe replay through the daemon's normal run machinery.

    TODO(seam): when ``TaskQueueService._build_worker_invocation`` accepts a
    per-task env override (e.g. ``task_item["env"]``), fold this into
    ``enqueue_tasks`` with ``goal=f"[recipe] {recipe.id}"`` and the same
    ``ARTEMIS_RECIPE_ID``/``ARTEMIS_RECIPE_VARS`` env — the worker module
    already reads both. Until then this mirrors that method's invocation
    recipe-for-recipe.
    """
    run_id = str(uuid.uuid4())
    variables = dict(variables or {})
    goal = f"[recipe] {recipe.name}"

    if device_serial:
        try:
            rejection = await device_pool.validate_explicit_serial_async(device_serial)
        except Exception:
            rejection = None
        if rejection:
            return {
                "status": "rejected",
                "error": rejection,
                "run_id": None,
                "tasks": [],
            }
    else:
        try:
            device_serial = await device_pool.select_device_async()
        except Exception:
            device_serial = None
        if not device_serial:
            raise HTTPException(503, "no Android device available for recipe run")

    endpoint = current_adb_endpoint()
    target = AdbTarget(endpoint=endpoint, serial=device_serial)
    cmd, env = _build_recipe_worker_invocation(
        recipe, run_id, device_serial, variables, target
    )

    # Hold the same FIFO queue ticket a pending task would, then transfer it
    # to the spawned worker (``_create_queue_item`` + ``_register_worker_run``).
    queue_ticket = DeviceExecutionLock.reserve(
        description=f"recipe task: {goal[:120]}",
        device_id=device_serial or "pending",
        session_id=run_id,
        ingress="recipe",
        lock_scope=target.lock_scope,
    )
    env[DeviceExecutionLock.QUEUE_TICKET_ENV] = str(queue_ticket)

    now = time.time()
    queue_item = {
        "session_id": run_id,
        "goal": goal,
        "profile": profile or "recipe",
        "expected_output": None,
        "enable_outputter": None,
        "verification_level": None,
        "explorer_mode": None,
        "locked_app_package": None,
        "app_path": None,
        "device_serial": device_serial,
        "adb_endpoint": endpoint.to_dict(),
        "ingress": "recipe",
        "conversation_id": None,
        "status": "running",
        "queue_ticket": queue_ticket,
        "created_at": now,
        "start_time": now,
    }

    print(f"[recipe-runs] Starting run {run_id}: recipe '{recipe.id}' on {device_serial}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(WORKSPACE_ROOT),
            env=env,
            **worker_process_io.subprocess_creation_kwargs(),
        )
    except Exception:
        DeviceExecutionLock.cancel_reservation(queue_ticket)
        raise
    queue_item["pid"] = proc.pid
    state.queue_items.append(queue_item)
    state.active_runs[run_id] = {
        "process": proc,
        "device_id": str(device_serial) if device_serial else None,
        "lock_key": target.lock_key if device_serial else None,
        "adb_endpoint": endpoint.to_dict(),
        "goal": goal,
        "profile": profile or "recipe",
    }
    try:
        trace_store.update_trace_pid(run_id, proc.pid)
    except OSError:
        pass
    DeviceExecutionLock.transfer_reservation(
        str(queue_ticket),
        proc.pid,
        description=f"recipe task: {goal[:120]}",
        device_id=device_serial or "pending",
        session_id=run_id,
        ingress="recipe",
        lock_scope=target.lock_scope,
    )

    _recipe_runs[run_id] = {
        "run_id": run_id,
        "session_id": run_id,
        "recipe_id": recipe.id,
        "recipe_name": recipe.name,
        "device_serial": device_serial,
        "variables": variables,
        "pid": proc.pid,
        "status": "running",
        "created_at": now,
        "start_time": now,
    }

    log_path = None
    try:
        log_path = trace_store.get_trace_stdout_log_path(run_id)
    except Exception:
        pass
    output_task = None
    if isinstance(proc.stdout, asyncio.StreamReader):
        output_task = asyncio.create_task(
            worker_process_io.forward_worker_output(proc.stdout, log_path)
        )
    watcher = asyncio.create_task(
        _watch_recipe_run(run_id, proc, queue_item, output_task)
    )
    _recipe_run_tasks.add(watcher)
    watcher.add_done_callback(_recipe_run_tasks.discard)

    return {
        "status": "started",
        "run_id": run_id,
        "session_id": run_id,
        "recipe_id": recipe.id,
        "device_serial": device_serial,
        "pid": proc.pid,
        "tasks": [queue_item],
    }


# --- Routes --------------------------------------------------------------------


@router.get("/api/recipes")
async def get_recipes():
    return {
        "recipes": [_recipe_summary(r) for r in list_recipes()],
        "ts": time.time(),
    }


class FromSessionRequest(BaseModel):
    session_id: str
    recipe_id: str
    name: str | None = None
    tags: list[str] | None = None
    param_hints: dict[str, Any] | None = None
    app_package: str | None = None
    """When set, the recipe opens with a ``launch_app`` precondition step."""


@router.post("/api/recipes/from-session")
async def create_recipe_from_session(req: FromSessionRequest):
    if not is_valid_recipe_id(req.recipe_id):
        raise HTTPException(400, f"invalid recipe id {req.recipe_id!r}")
    recorder = RecipeRecorder()
    try:
        recipe = await run_in_threadpool(
            recorder.from_session,
            req.session_id,
            req.recipe_id,
            req.param_hints,
            req.name,
            req.tags,
            req.app_package,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None
    path = await run_in_threadpool(recorder.save, recipe)
    return {"ok": True, "recipe": _recipe_summary(recipe), "path": str(path)}


@router.get("/api/recipes/runs/{run_id}")
async def get_recipe_run(run_id: str):
    result = _read_run_result(run_id)
    if result is not None:
        return result
    record = _recipe_runs.get(run_id)
    if record is not None:
        return record
    raise HTTPException(404, f"recipe run not found: {run_id}")


@router.get("/api/recipes/{recipe_id}")
async def get_recipe(recipe_id: str):
    return _load_or_404(recipe_id).model_dump(mode="json")


@router.put("/api/recipes/{recipe_id}")
async def update_recipe(recipe_id: str, recipe: Recipe):
    """Replaces a recipe wholesale — the hand-edit path (preconditions, asserts).

    The body must be a complete recipe; FastAPI/pydantic validation rejects a
    malformed one with 422 before anything touches disk.
    """
    _load_or_404(recipe_id)
    if recipe.id != recipe_id:
        raise HTTPException(
            400, f"recipe id mismatch: path {recipe_id!r} vs body {recipe.id!r}"
        )
    path = await run_in_threadpool(RecipeRecorder().save, recipe)
    return {"ok": True, "recipe": _recipe_summary(recipe), "path": str(path)}


@router.delete("/api/recipes/{recipe_id}")
async def remove_recipe(recipe_id: str):
    try:
        removed = delete_recipe(recipe_id)
    except ValueError:
        raise HTTPException(400, f"invalid recipe id {recipe_id!r}") from None
    if not removed:
        raise HTTPException(404, f"recipe not found: {recipe_id}")
    return {"ok": True, "id": recipe_id}


class RecipeRunRequest(BaseModel):
    device_serial: str | None = None
    variables: dict[str, Any] | None = None
    profile: str | None = None


@router.post("/api/recipes/{recipe_id}/run")
async def run_recipe(recipe_id: str, req: RecipeRunRequest):
    recipe = _load_or_404(recipe_id)
    if not recipe.steps:
        raise HTTPException(400, f"recipe {recipe.id} has no replayable steps")
    return await enqueue_recipe_run(
        recipe,
        device_serial=req.device_serial,
        variables=req.variables,
        profile=req.profile,
    )
