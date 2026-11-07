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

"""Recipe run worker — the subprocess sibling of ``artemis.main``.

Spawned by the admin console as ``python -m artemis.recipes.worker`` with the
same worker environment the task queue prepares (``ARTEMIS_SESSION_ID``,
``ARTEMIS_TASK_WORKER``, ``ARTEMIS_DEVICE_QUEUE_TICKET``, ``ADB_DEVICE_SERIAL``)
plus the recipe payload (``ARTEMIS_RECIPE_ID`` / ``ARTEMIS_RECIPE_VARS``).

A recipe run is a normal device-queue citizen: it takes the shared FIFO ticket
the daemon reserved, waits its turn on the per-device ``DeviceExecutionLock``,
then replays the recipe through ``RecipeRunner`` — a real worker process, so
the daemon's stop/cancel path (marker file -> graceful cancel -> hard-kill
fallback) works exactly like it does for ``artemis.main`` workers.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from artemis.config import DEFAULT_ADB_HOST, DEFAULT_ADB_PORT, settings
from artemis.config.paths import TRACES_PATH
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform, ExecutionSetup
from artemis.recipes.models import RecipeRunResult
from artemis.recipes.recorder import RECIPE_RUNS_DIR, load_recipe
from artemis.recipes.runner import RecipeRunner
from artemis.runtime import DeviceExecutionLock, device_pool
from artemis.runtime.cancel_requests import clear_cancel_request, watch_for_cancel_request
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def _write_run_result(result: RecipeRunResult) -> Path | None:
    """Persists the run outcome next to the recipe store for the daemon to read."""
    try:
        RECIPE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        path = RECIPE_RUNS_DIR / f"{result.run_id}.json"
        path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        logger.warning(f"Could not persist recipe run result: {exc}")
        return None


async def _connect_screen_client(client) -> None:
    """Connects the screen client off the event loop (helper install can take seconds)."""
    connect = getattr(client, "connect", None)
    if not callable(connect):
        return
    try:
        accepts_events = "on_event" in inspect.signature(connect).parameters
    except (TypeError, ValueError):
        accepts_events = False
    if accepts_events:
        await asyncio.to_thread(connect, on_event=None)
    else:
        await asyncio.to_thread(connect)


async def _acquire_device_lease(lock: DeviceExecutionLock) -> None:
    """Blocks on the shared FIFO queue; survives a cancel that lands mid-wait.

    Mirrors the agent's drain dance: ``asyncio.to_thread`` cannot kill the
    blocking acquire, so the lock's own ``cancel_event`` ends the wait cleanly
    and the already-finished acquire result is drained before propagating.
    """
    queue_cancel_event = threading.Event()
    acquire_task = asyncio.create_task(
        asyncio.to_thread(lock.acquire, cancel_event=queue_cancel_event)
    )
    try:
        await asyncio.shield(acquire_task)
    except asyncio.CancelledError:
        queue_cancel_event.set()
        try:
            await asyncio.shield(acquire_task)
        except Exception as exc:
            logger.debug(f"Device lock acquisition drain after cancel failed: {exc}")
        raise


async def _execute_recipe(recipe, run_id: str, device_serial: str, variables: dict) -> int:
    """Holds the device lease and replays the recipe; returns the exit code."""
    from adbutils import AdbClient

    from artemis.clients.screen_client_factory import create_screen_client
    from artemis.data_engine.engine import DataEngine
    from artemis.platform import platform as pal_platform

    lock = DeviceExecutionLock(
        device_serial,
        description=f"recipe run: {recipe.id}",
        session_id=run_id,
        ingress="recipe",
    )
    await _acquire_device_lease(lock)

    ui_client = None
    result: RecipeRunResult | None = None
    try:
        adb_client = AdbClient(
            host=settings.ADB_HOST or DEFAULT_ADB_HOST,
            port=settings.ADB_PORT or DEFAULT_ADB_PORT,
        )
        try:
            width, height = await asyncio.to_thread(
                adb_client.device(device_serial).window_size
            )
        except Exception as exc:
            logger.warning(f"Could not read window size for {device_serial}: {exc}")
            width, height = 1080, 2400

        ui_client = create_screen_client(device_serial)
        await _connect_screen_client(ui_client)

        device = DeviceContext(
            host_platform=pal_platform.os_type.name,
            mobile_platform=DevicePlatform.ANDROID,
            device_id=device_serial,
            device_width=width,
            device_height=height,
        )
        ctx = ArtemisContext(device=device, adb_client=adb_client, ui_adb_client=ui_client)
        ctx.execution_setup = ExecutionSetup(
            traces_path=Path(TRACES_PATH),
            trace_name=f"recipe_{int(time.time())}_{run_id[:8]}",
        )
        ctx.data_engine = DataEngine(ctx=ctx)
        ctx.data_engine.start_session(
            goal=f"[recipe] {recipe.name}",
            device_info=device.model_dump(),
            session_id=uuid.UUID(run_id),
        )
        try:
            async with ctx:
                try:
                    result = await RecipeRunner(ctx).run(recipe, variables, run_id=run_id)
                    ctx.data_engine.end_session("completed" if result.success else "failed")
                except asyncio.CancelledError:
                    ctx.data_engine.end_session("cancelled")
                    _write_run_result(
                        RecipeRunResult(
                            run_id=run_id,
                            recipe_id=recipe.id,
                            recipe_name=recipe.name,
                            session_id=run_id,
                            device_serial=device_serial,
                            error="cancelled",
                        )
                    )
                    raise
                except BaseException:
                    ctx.data_engine.end_session("failed")
                    raise
        finally:
            if ui_client is not None:
                try:
                    await asyncio.to_thread(
                        getattr(ui_client, "disconnect", lambda: None)
                    )
                except Exception:
                    pass

        if result is not None:
            _write_run_result(result)
        return 0 if (result and result.success) else 1
    finally:
        lock.release()


async def _amain() -> int:
    parser = argparse.ArgumentParser(prog="artemis.recipes.worker")
    parser.add_argument("--recipe", default=None, help="Recipe id (or ARTEMIS_RECIPE_ID).")
    parser.add_argument("--run-id", default=None, help="Run/session UUID.")
    parser.add_argument("--device-serial", default=None)
    parser.add_argument("--vars", default=None, help="Variable overrides as JSON.")
    args = parser.parse_args()

    recipe_id = args.recipe or os.getenv("ARTEMIS_RECIPE_ID")
    if not recipe_id:
        print("[recipe-worker] no recipe id (use --recipe or ARTEMIS_RECIPE_ID)")
        return 2
    try:
        recipe = load_recipe(recipe_id)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[recipe-worker] {exc}")
        return 2

    run_id = args.run_id or os.getenv("ARTEMIS_SESSION_ID") or uuid.uuid4().hex
    try:
        uuid.UUID(run_id)
    except ValueError:
        print(f"[recipe-worker] run id must be a UUID (it doubles as session id): {run_id}")
        return 2

    variables = {}
    raw_vars = args.vars or os.getenv("ARTEMIS_RECIPE_VARS")
    if raw_vars:
        try:
            variables = json.loads(raw_vars)
        except ValueError:
            print(f"[recipe-worker] malformed variable JSON: {raw_vars[:200]!r}")
            return 2

    device_serial = args.device_serial or os.getenv("ADB_DEVICE_SERIAL")
    if not device_serial:
        try:
            device_serial = await device_pool.select_device_async()
        except Exception:
            device_serial = None
    if not device_serial:
        print("[recipe-worker] no Android device available")
        return 1

    print(
        f"[recipe-worker] run {run_id}: recipe '{recipe.id}' "
        f"({len(recipe.steps)} steps) on {device_serial}"
    )

    run_task = asyncio.create_task(_execute_recipe(recipe, run_id, device_serial, variables))
    watcher = asyncio.create_task(
        watch_for_cancel_request(run_task.cancel, session_id=run_id)
    )
    try:
        return await run_task
    except asyncio.CancelledError:
        # The daemon dropped a cancel marker; the worker finalizes like the
        # agent does (session status -> cancelled, result file written).
        return 130
    finally:
        watcher.cancel()
        clear_cancel_request(session_id=run_id, pid=os.getpid())


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
