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

"""Deterministic recipe replay — zero LLM calls.

``RecipeRunner`` replays a recipe's wire calls through the same in-process
action session the Validator uses: each step's ``action``/``args`` is already
the canonical ``(tool_name, wire_args)`` pair ``exec_action`` resolves via
``to_canonical_call``, so the runner's dispatch is a plain
``session.call(action, args)`` — no precondition nets, no retries, no model.
A run aborts on the first failed step: replay is deterministic only while the
device still matches the recording, and a failed action leaves it unknown.

Per-step ``timeout_ms`` wraps the call in ``asyncio.wait_for``: abandoning the
caller future is safe here (the transport call completes inside the session's
owner task — see ``ActionSession`` docs), and the runner aborts the run rather
than interleaving later steps with an in-flight action.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any
import uuid

from artemis.config.paths import TRACES_PATH
from artemis.recipes.models import (
    VARIABLE_PATTERN,
    Recipe,
    RecipeRunResult,
    RecipeStep,
    RecipeStepResult,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["RecipeRunner"]

#: Fallback shot store when no data engine is attached. Duplicates
#: ``recorder.RECIPE_RUNS_DIR`` on purpose: importing the recorder pulls in the
#: history/action-spec stack (langchain), which a zero-LLM runner must not need.
_RUN_SHOTS_DIR = Path(TRACES_PATH) / "recipes" / "runs" / "shots"


def substitute_vars(value: Any, variables: dict[str, Any]) -> Any:
    """Recursively replaces ``{{name}}`` placeholders in string values.

    Unknown names are left verbatim so a mistyped placeholder surfaces as
    itself in the run result instead of disappearing silently.
    """
    if isinstance(value, dict):
        return {k: substitute_vars(v, variables) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [substitute_vars(v, variables) for v in value]
    if not isinstance(value, str) or "{{" not in value:
        return value
    return VARIABLE_PATTERN.sub(
        lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0),
        value,
    )


class RecipeRunner:
    """Replays a :class:`Recipe` against ``ctx``'s device through its action session.

    ``ctx`` only needs ``action_session`` (or the fields ``get_action_session``
    builds one from — ``device``/clients) and optionally ``data_engine`` for
    step/screenshot persistence; an :class:`~artemis.context.ArtemisContext`
    satisfies both.
    """

    def __init__(self, ctx: Any):
        self.ctx = ctx

    async def _session(self):
        session = getattr(self.ctx, "action_session", None)
        if session is None or not getattr(session, "started", False):
            from artemis.mcp.action_session import get_action_session

            session = await get_action_session(self.ctx)
        return session

    # --- asserts ---------------------------------------------------------

    async def _persist_screenshot(self, run_id: str, step_index: int, b64: str) -> str | None:
        """Stores an assert-captured screenshot; returns its lookup name."""
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return None, None
        engine = getattr(self.ctx, "data_engine", None)
        if engine is not None:
            try:
                return engine.get_or_create_image(raw), raw
            except Exception as exc:
                logger.debug(f"Could not persist recipe screenshot to data engine: {exc}")
        try:
            shots = _RUN_SHOTS_DIR / run_id
            shots.mkdir(parents=True, exist_ok=True)
            name = f"step_{step_index:02d}.jpg"
            await asyncio.to_thread((shots / name).write_bytes, raw)
            return name, raw
        except OSError as exc:
            logger.debug(f"Could not persist recipe screenshot: {exc}")
            return None, None

    async def _check_assert(
        self,
        session: Any,
        step: RecipeStep,
        run_id: str,
        step_index: int,
        variables: dict[str, Any],
    ) -> tuple[bool, str | None, str | None, bytes | None]:
        """Runs one ``assert_after``; returns (passed, error, shot_name, shot_bytes)."""
        spec = step.assert_after or {}
        kind = str(spec.get("type") or "")
        timeout_ms = int(spec.get("timeout_ms") or step.timeout_ms)

        if kind in ("text_present", "text_absent"):
            text = substitute_vars(str(spec.get("text") or ""), variables)
            if not text:
                return False, "text assert missing 'text'", None, None
            res = await session.call(
                "wait_for_text",
                {
                    "text": text,
                    "wait_state": "appear" if kind == "text_present" else "disappear",
                    "timeout_ms": timeout_ms,
                },
            )
            return (res.ok, None if res.ok else res.message, None, None)

        if kind == "screenshot":
            try:
                b64 = await session.screenshot_b64(timeout=timeout_ms / 1000)
            except Exception as exc:
                return False, f"screenshot assert failed: {exc}", None, None
            name, raw = await self._persist_screenshot(run_id, step_index, b64)
            return True, None, name, raw

        return False, f"unknown assert_after type: {kind!r}", None, None

    # --- recording --------------------------------------------------------

    def _record_step(
        self,
        step: RecipeStep,
        args: dict[str, Any],
        outcome: RecipeStepResult,
        post_screenshot_bytes: bytes | None,
    ) -> None:
        """Mirrors the executor's step/trace records when a data engine is attached."""
        engine = getattr(self.ctx, "data_engine", None)
        if engine is None or not getattr(engine, "current_session_id", None):
            return
        try:
            step_id = engine.record_step(
                post_screenshot_bytes=post_screenshot_bytes,
                action_taken={
                    "action": step.action,
                    "coordinate_space": "normalized",
                    "args": args,
                },
                last_execution_result={
                    "status": "dispatched" if outcome.success else "failed",
                    "result": outcome.error or f"recipe step {outcome.step_index} dispatched",
                },
                extra_metadata={"recipe_id": getattr(self, "_recipe_id", None)},
            )
            engine.record_trace(
                type="action",
                name=step.action,
                step_id=step_id,
                status="success" if outcome.success else "failed",
                duration=outcome.elapsed_ms / 1000.0,
                payload={
                    "action": {**args, "action": step.action},
                    "success": outcome.success,
                    "error_msg": outcome.error,
                    "post_screenshot": outcome.post_screenshot_name,
                    "timestamp": time.time(),
                    "relative_time": engine.get_relative_time(time.time()),
                    "recipe": True,
                },
            )
        except Exception as exc:
            logger.debug(f"Recipe step record skipped: {exc}")

    # --- run ---------------------------------------------------------------

    async def run(
        self,
        recipe: Recipe,
        variables: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> RecipeRunResult:
        """Replays every step in order; aborts on the first failed step/assert.

        ``run_id`` lets the caller pin the result's id (the worker passes the
        session/run id so the daemon can find the persisted result file).
        """
        merged_vars = {**recipe.variables, **(variables or {})}
        run_id = run_id or uuid.uuid4().hex
        self._recipe_id = recipe.id
        session = await self._session()
        device_serial = getattr(getattr(self.ctx, "device", None), "device_id", None)

        started = datetime.now().astimezone()
        t_start = time.monotonic()
        outcomes: list[RecipeStepResult] = []
        failed_index: int | None = None
        run_error: str | None = None

        for index, step in enumerate(recipe.steps):
            args = substitute_vars(step.args, merged_vars)
            outcome = RecipeStepResult(step_index=index, action=step.action, args=args)
            outcomes.append(outcome)

            # A wait_for_delay step legitimately outlives timeout_ms; its own
            # duration is added on top of the per-step budget.
            budget_ms = step.timeout_ms + (
                int(args.get("time_in_ms") or 0) if step.action == "wait_for_delay" else 0
            )
            t0 = time.monotonic()
            try:
                res = await asyncio.wait_for(
                    session.call(step.action, args), timeout=max(1.0, budget_ms / 1000.0)
                )
                outcome.success, outcome.error = res.ok, (None if res.ok else res.message)
            except TimeoutError:
                outcome.success = False
                outcome.error = (
                    f"step timed out after {budget_ms}ms "
                    "(device call may still be in flight; aborting run)"
                )
            except Exception as exc:
                outcome.success = False
                outcome.error = str(exc)
            outcome.elapsed_ms = int((time.monotonic() - t0) * 1000)

            shot_name: str | None = None
            shot_bytes: bytes | None = None
            if outcome.success and step.assert_after:
                t_assert = time.monotonic()
                passed, assert_err, shot_name, shot_bytes = await self._check_assert(
                    session, step, run_id, index, merged_vars
                )
                outcome.elapsed_ms += int((time.monotonic() - t_assert) * 1000)
                outcome.assert_passed = passed
                outcome.post_screenshot_name = shot_name
                if not passed:
                    outcome.success = False
                    outcome.error = f"assert_after failed: {assert_err}"
            elif step.assert_after is not None:
                outcome.assert_passed = None

            self._record_step(step, args, outcome, shot_bytes)

            if not outcome.success:
                failed_index = index
                run_error = outcome.error
                logger.warning(
                    f"Recipe {recipe.id} aborted at step {index} ({step.action}): {outcome.error}"
                )
                break

        finished = datetime.now().astimezone()
        return RecipeRunResult(
            run_id=run_id,
            recipe_id=recipe.id,
            recipe_name=recipe.name,
            session_id=(
                str(getattr(self.ctx.data_engine, "current_session_id"))
                if getattr(self.ctx, "data_engine", None)
                and getattr(self.ctx.data_engine, "current_session_id", None)
                else None
            ),
            device_serial=device_serial,
            started_at=started.isoformat(timespec="seconds"),
            finished_at=finished.isoformat(timespec="seconds"),
            elapsed_ms=int((time.monotonic() - t_start) * 1000),
            success=failed_index is None,
            failed_step_index=failed_index,
            error=run_error,
            variables=merged_vars,
            steps=outcomes,
        )
