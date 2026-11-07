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
``session.call(action, args)`` — no retries, no model.
A run aborts on the first failed step: replay is deterministic only while the
device still matches the recording, and a failed action leaves it unknown.

Before a step's action an optional ``precondition`` runs through the same
channel: ``launch_app`` foregrounds a package and ``wait_for_text`` blocks on
a screen string (either failure aborts the step — the recorded coordinates
mean nothing without that state), while ``if_text_then_tap`` is a conditional
dismiss that taps the text's element when present and otherwise continues
silently (first-run onboarding is the target). After the action an
``assert_after`` check is recorded in the step result; a failed check only
aborts the run when the spec declares ``"required": true``.

Per-step ``timeout_ms`` wraps the call in ``asyncio.wait_for``: abandoning the
caller future is safe here (the transport call completes inside the session's
owner task — see ``ActionSession`` docs), and the runner aborts the run rather
than interleaving later steps with an in-flight action.

Two opt-in surfaces extend the deterministic core:

* ``@workspace`` references — a step arg (or ``{{var}}`` value) of the form
  ``@workspace:<serial>/<path>`` resolves into the device's durable
  workspace under ``traces/workspaces/``; the runner-side ``inject_file``
  action (``{workspace_path, device_path}``) adb-pushes it before the steps
  that consume it. The workspace is ensured before each run and gets one
  ``memories.md`` line after it.
* ``on_fail: "vision"`` — when the step's action or a ``required`` assert
  fails, exactly one bounded VLM call proposes a replacement ActionSpec; a
  successful repair marks the step ``repaired`` and rewrites the step in the
  recipe file (logged in ``recipe.patches``). Any other value — and the
  default — keeps replay deterministic with zero LLM calls.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import json
from pathlib import Path
import re
import time
from typing import Any
import uuid

from artemis.config.paths import TRACES_PATH
from artemis.mcp.action_types import ActionCode, ActionResult
from artemis.recipes.models import (
    VARIABLE_PATTERN,
    Recipe,
    RecipeRunResult,
    RecipeStep,
    RecipeStepResult,
)
from artemis.recipes.workspaces import (
    append_memory,
    ensure_workspace,
    is_workspace_ref,
    memory_line,
    resolve_workspace_path,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["RecipeRunner"]

#: Fallback shot store when no data engine is attached. Duplicates
#: ``recorder.RECIPE_RUNS_DIR`` on purpose: importing the recorder pulls in the
#: history/action-spec stack (langchain), which a zero-LLM runner must not need.
_RUN_SHOTS_DIR = Path(TRACES_PATH) / "recipes" / "runs" / "shots"

#: ``[x1,y1][x2,y2]`` pixel bounds on a hierarchy element.
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

#: Prompt for the bounded ``on_fail: "vision"`` repair lane. The model sees the
#: failed step + a live screenshot and must emit exactly one ActionSpec JSON.
_VISION_REPAIR_PROMPT = """You are repairing one step of a deterministic UI \
replay on an Android device. The recorded step below just failed. Look at the \
current screenshot and propose ONE replacement device action that accomplishes \
the step's intent from the state you see.

Reply with ONLY a JSON object — no prose, no code fences:
{"action": "<tool>", "args": {<wire args>}}

Wire dialect (same as the recipe's steps):
- click {"target": [x, y]}                    x,y are 0-1000 normalized coords
- long_press {"target": [x, y], "duration_ms": int}
- input_text {"text": "...", "clear_exist": bool, "target": [x, y]?}
- swipe {"start": [x, y], "end": [x, y], "duration_ms": int}
- press_key {"key": "back"|"home"|"enter"|"delete"|"tab"|"search"|"menu"|"app_switch"}
- click_sequence {"sequence": [[x, y], ...], "delay_ms": int}
- wait_for_delay {"time_in_ms": int}
- manage_app {"action": "launch"|"stop", "app_name": "<package>"}
- open_link {"url": "..."}

If nothing on screen can fix the step, emit your single best guess anyway —
a click on the most plausible element is better than no answer."""


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


def resolve_asset_args(value: Any, device_serial: str | None) -> Any:
    """Rewrites ``@workspace`` file references to absolute local paths.

    A variable or literal arg of the form ``@workspace:<serial>/<path>`` (or
    ``@workspace/<path>`` for the running device) resolves into that device's
    durable workspace — the file can then be pushed (``inject_file``) or fed
    to an action taking an image path. A missing file or a ``..`` escape is an
    authoring error: it raises, which fails the step like any bad arg.
    """
    if isinstance(value, dict):
        return {k: resolve_asset_args(v, device_serial) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [resolve_asset_args(v, device_serial) for v in value]
    if not is_workspace_ref(value):
        return value
    if not device_serial:
        raise ValueError(
            f"workspace reference {value!r} needs a device (run has no serial)"
        )
    return str(resolve_workspace_path(device_serial, value))


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

    # --- preconditions ------------------------------------------------------

    def _device_dims(self) -> tuple[int, int]:
        device = getattr(self.ctx, "device", None)
        width = getattr(device, "device_width", 1080) if device else 1080
        height = getattr(device, "device_height", 2400) if device else 2400
        return width or 1080, height or 2400

    @staticmethod
    def _element_bounds(elem: dict[str, Any]) -> tuple[int, int, int, int] | None:
        """Pixel ``(x1, y1, x2, y2)`` for one hierarchy element, or None."""
        parsed = elem.get("parsed_bounds")
        if isinstance(parsed, dict):
            try:
                return (
                    int(parsed["left"]),
                    int(parsed["top"]),
                    int(parsed["right"]),
                    int(parsed["bottom"]),
                )
            except (KeyError, TypeError, ValueError):
                pass
        bounds = elem.get("bounds")
        if isinstance(bounds, str):
            match = _BOUNDS_RE.match(bounds)
            if match:
                return tuple(map(int, match.groups()))
        return None

    def _find_text_element(
        self, elements: Any, needle: str
    ) -> tuple[int, int] | None:
        """Pixel center of the best element carrying ``needle`` (case-insensitive).

        Matches ``text``/``content-desc`` and prefers clickable elements, then
        the smallest bounds — the leaf-most match, so a label inside a button
        wins over the window container.
        """
        low = needle.lower()
        best: tuple[int, int, int, int] | None = None  # (rank, area, cx, cy)
        for elem in elements or []:
            if not isinstance(elem, dict):
                continue
            haystacks = (
                elem.get("text"),
                elem.get("content-desc"),
                elem.get("accessibilityText"),
            )
            if not any(low in str(h).lower() for h in haystacks if h):
                continue
            bounds = self._element_bounds(elem)
            if bounds is None:
                continue
            x1, y1, x2, y2 = bounds
            area = max(0, x2 - x1) * max(0, y2 - y1)
            rank = 0 if str(elem.get("clickable")) == "true" else 1
            candidate = (rank, area, (x1 + x2) // 2, (y1 + y2) // 2)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        return (best[2], best[3]) if best else None

    async def _tap_text(self, session: Any, text: str, timeout_ms: int) -> bool:
        """Polls the hierarchy for ``text`` and taps its element when found.

        Returns ``True`` once a tap dispatches; ``False`` when the deadline
        passes with no match — the element may simply never appear (e.g. the
        app was already past onboarding), which callers treat as "nothing to
        dismiss" rather than an error.
        """
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        width, height = self._device_dims()
        while True:
            try:
                elements = await session.ui_hierarchy(timeout=5.0)
            except Exception as exc:
                logger.debug(f"hierarchy poll failed during if_text_then_tap: {exc}")
                elements = []
            center = self._find_text_element(elements, text)
            if center is not None:
                nx = max(0, min(1000, round(center[0] * 1000 / width)))
                ny = max(0, min(1000, round(center[1] * 1000 / height)))
                try:
                    res = await session.call("click", {"target": [nx, ny]})
                except Exception as exc:
                    res = None
                    logger.debug(f"if_text_then_tap tap call raised: {exc}")
                if res is not None and res.ok:
                    logger.info(
                        f"if_text_then_tap dismissed '{text}' at [{nx}, {ny}]"
                    )
                    return True
                logger.warning(
                    f"if_text_then_tap tap on '{text}' failed"
                    f" ({getattr(res, 'message', None)}); retrying until deadline"
                )
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def _check_precondition(
        self,
        session: Any,
        step: RecipeStep,
        variables: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Evaluates one ``precondition``; returns (passed, error).

        ``launch_app``/``wait_for_text`` report real failures (the caller
        aborts the step — recorded coordinates mean nothing without that
        state); ``if_text_then_tap`` is a conditional dismiss and always
        reports ``True`` once evaluated.
        """
        spec = step.precondition or {}
        kind = str(spec.get("type") or "")
        timeout_ms = int(spec.get("timeout_ms") or step.timeout_ms)

        if kind == "launch_app":
            package = substitute_vars(
                str(spec.get("package") or spec.get("app_name") or ""), variables
            )
            if not package:
                return False, "launch_app precondition missing 'package'"
            res = await session.call(
                "manage_app", {"action": "launch", "app_name": package}
            )
            return (res.ok, None if res.ok else res.message)

        if kind == "wait_for_text":
            text = substitute_vars(str(spec.get("text") or ""), variables)
            if not text:
                return False, "wait_for_text precondition missing 'text'"
            res = await session.call(
                "wait_for_text",
                {
                    "text": text,
                    "wait_state": str(spec.get("wait_state") or "appear"),
                    "timeout_ms": timeout_ms,
                },
            )
            return (res.ok, None if res.ok else res.message)

        if kind == "if_text_then_tap":
            text = substitute_vars(str(spec.get("text") or ""), variables)
            if not text:
                return False, "if_text_then_tap precondition missing 'text'"
            tapped = await self._tap_text(session, text, timeout_ms)
            if not tapped:
                logger.debug(f"if_text_then_tap: '{text}' not on screen; continuing")
            return True, None

        return False, f"unknown precondition type: {kind!r}"

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

    # --- file injection ----------------------------------------------------

    async def _inject_file(
        self, args: dict[str, Any], device_serial: str | None
    ) -> ActionResult:
        """``inject_file`` step: pushes a workspace file to the device.

        Runner-side action (no MCP tool): ``adb push`` through the context's
        adb client, so recipes can stage images/payloads before the steps that
        consume them. Args: ``workspace_path`` (``@workspace`` ref, ``assets/``
        name, or absolute path under the workspaces root) and ``device_path``
        (absolute path on the device).
        """
        workspace_path = str(args.get("workspace_path") or "")
        device_path = str(args.get("device_path") or "")
        if not device_serial:
            return ActionResult.failure(
                "inject_file", "no device serial for this run", ActionCode.INVALID_ARGS
            )
        if not workspace_path or not device_path.startswith("/"):
            return ActionResult.failure(
                "inject_file",
                "inject_file needs 'workspace_path' and an absolute 'device_path'",
                ActionCode.INVALID_ARGS,
            )
        try:
            local = resolve_workspace_path(device_serial, workspace_path)
        except (ValueError, FileNotFoundError) as exc:
            return ActionResult.failure(
                "inject_file", str(exc), ActionCode.INVALID_ARGS
            )
        if local.is_dir():
            return ActionResult.failure(
                "inject_file",
                f"workspace_path {workspace_path!r} is a directory",
                ActionCode.INVALID_ARGS,
            )
        adb_client = getattr(self.ctx, "adb_client", None)
        if adb_client is None:
            return ActionResult.failure(
                "inject_file", "ctx has no adb_client", ActionCode.DEVICE_ERROR
            )
        try:
            device = adb_client.device(device_serial)
            await asyncio.to_thread(device.sync.push, str(local), device_path)
        except Exception as exc:
            return ActionResult.failure(
                "inject_file",
                f"adb push failed: {exc}",
                ActionCode.DEVICE_ERROR,
            )
        logger.info(
            f"inject_file pushed {local} -> {device_serial}:{device_path}"
        )
        return ActionResult.success(
            "inject_file",
            f"pushed {local.name} to {device_path}",
        )

    # --- bounded vision repair (opt-in per step) ---------------------------

    async def _propose_repair_action(
        self,
        screenshot_b64: str,
        step: RecipeStep,
        args: dict[str, Any],
        outcome: RecipeStepResult,
    ) -> tuple[str, dict[str, Any]] | None:
        """One VLM call proposing a replacement ActionSpec; ``None`` on any
        failure — the caller then fails the step normally.

        Imports live inside so the deterministic path never pays for the LLM
        stack; this lane only runs when the step opts in via ``on_fail``.
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        from artemis.mcp.action_manifest import DEVICE_ACTIONS, INTERNAL_ACTIONS
        from artemis.services.llm import acomplete_structured, get_llm

        allowed = sorted(DEVICE_ACTIONS - INTERNAL_ACTIONS)
        failed = {
            "step_index": outcome.step_index,
            "action": step.action,
            "args": args,
            "description": step.description,
            "error": outcome.error,
        }
        messages = [
            SystemMessage(content=_VISION_REPAIR_PROMPT),
            HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": (
                            "Failed step (JSON):\n"
                            + json.dumps(failed, ensure_ascii=False)
                            + f"\nAllowed tools: {', '.join(allowed)}"
                            + "\nCurrent screenshot:"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{screenshot_b64}"
                        },
                    },
                ]
            ),
        ]
        try:
            llm = get_llm(self.ctx, name="validator_pixel_safety_net")
        except Exception as exc:
            logger.warning(f"vision repair unavailable (no llm): {exc}")
            return None
        try:
            parsed = await asyncio.wait_for(
                acomplete_structured(llm, messages), timeout=60.0
            )
        except Exception as exc:
            logger.warning(f"vision repair call failed: {exc}")
            return None
        if not isinstance(parsed, dict):
            logger.warning(f"vision repair returned non-object: {parsed!r}")
            return None
        action = str(parsed.get("action") or "")
        new_args = parsed.get("args")
        if action not in allowed or not isinstance(new_args, dict):
            logger.warning(
                f"vision repair proposed invalid spec: {parsed!r} "
                f"(allowed: {allowed})"
            )
            return None
        return action, new_args

    def _persist_recipe_patch(
        self,
        recipe: Recipe,
        index: int,
        new_action: str,
        new_args: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any] | None:
        """Writes the successful repair back into the recipe file.

        The step's ``action``/``args`` are replaced in place and an entry is
        appended to ``recipe.patches`` so the heal is auditable — next replay
        is deterministic again. Import of the recorder is lazy: it pulls in
        the history/action-spec stack a zero-LLM run must not need.
        """
        from artemis.recipes.recorder import recipe_path

        try:
            step = recipe.steps[index]
            patch = {
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "run_id": run_id,
                "step_index": index,
                "kind": "vision",
                "old_action": step.action,
                "old_args": step.args,
                "new_action": new_action,
                "new_args": new_args,
            }
            step.action, step.args = new_action, new_args
            recipe.patches.append(patch)
            path = recipe_path(recipe.id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    recipe.model_dump(mode="json"), indent=2, ensure_ascii=False
                )
                + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
            logger.info(
                f"recipe {recipe.id}: vision patch persisted at step {index} "
                f"({patch['old_action']} -> {new_action})"
            )
            return patch
        except Exception as exc:
            logger.warning(f"Could not persist recipe patch: {exc}")
            return None

    async def _attempt_step_repair(
        self,
        session: Any,
        recipe: Recipe,
        index: int,
        step: RecipeStep,
        args: dict[str, Any],
        outcome: RecipeStepResult,
        run_id: str,
    ) -> dict[str, Any] | None:
        """The ``on_fail`` fallback; ``None`` means "fail the step normally".

        ``"vision"``: screenshot -> one VLM call for a replacement ActionSpec ->
        dispatch it -> persist the fix. Exactly one shot — a second failure is
        final, keeping replay bounded and effectively deterministic.
        """
        if step.on_fail != "vision":
            return None
        from_error = outcome.error
        try:
            b64 = await session.screenshot_b64(timeout=10.0)
        except Exception as exc:
            logger.warning(f"vision repair needs a screenshot; failed: {exc}")
            return None
        spec = await self._propose_repair_action(b64, step, args, outcome)
        if spec is None:
            return None
        action, new_args = spec
        t_repair = time.monotonic()
        try:
            res = await asyncio.wait_for(
                session.call(action, new_args),
                timeout=max(1.0, step.timeout_ms / 1000.0),
            )
        except Exception as exc:
            logger.warning(f"vision repair action {action} failed: {exc}")
            return None
        if not res.ok:
            logger.warning(
                f"vision repair action {action} was rejected: {res.message}"
            )
            return None
        patch = self._persist_recipe_patch(
            recipe, index, action, new_args, run_id
        )
        return {
            "kind": "vision",
            "action": action,
            "args": new_args,
            "from_error": from_error,
            "persisted": patch is not None,
            "elapsed_ms": int((time.monotonic() - t_repair) * 1000),
        }

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
        """Replays every step in order; aborts on the first failed step,
        failed precondition, or failed ``required`` assert.

        ``run_id`` lets the caller pin the result's id (the worker passes the
        session/run id so the daemon can find the persisted result file).
        """
        merged_vars = {**recipe.variables, **(variables or {})}
        run_id = run_id or uuid.uuid4().hex
        self._recipe_id = recipe.id
        session = await self._session()
        device_serial = getattr(getattr(self.ctx, "device", None), "device_id", None)
        if device_serial:
            ensure_workspace(device_serial)

        started = datetime.now().astimezone()
        t_start = time.monotonic()
        outcomes: list[RecipeStepResult] = []
        failed_index: int | None = None
        run_error: str | None = None

        for index, step in enumerate(recipe.steps):
            args = substitute_vars(step.args, merged_vars)
            outcome = RecipeStepResult(step_index=index, action=step.action, args=args)
            outcomes.append(outcome)

            try:
                args = resolve_asset_args(args, device_serial)
                outcome.args = args
            except (ValueError, FileNotFoundError) as exc:
                # A bad workspace reference is an authoring error — it fails
                # the step like any invalid argument and aborts the run.
                outcome.error = str(exc)
                self._record_step(step, args, outcome, None)
                failed_index = index
                run_error = outcome.error
                logger.warning(
                    f"Recipe {recipe.id} aborted at step {index} "
                    f"({step.action}): {outcome.error}"
                )
                break

            # A wait_for_delay step legitimately outlives timeout_ms; its own
            # duration is added on top of the per-step budget.
            budget_ms = step.timeout_ms + (
                int(args.get("time_in_ms") or 0) if step.action == "wait_for_delay" else 0
            )

            if step.precondition:
                spec = step.precondition
                # Generous outer bound: spec timeouts get a poll margin, and a
                # launching app may need launch_app_with_retries' full ~45s.
                pre_budget_ms = max(
                    budget_ms, int(spec.get("timeout_ms") or 0) + 15_000
                )
                if str(spec.get("type") or "") == "launch_app":
                    pre_budget_ms = max(pre_budget_ms, 60_000)
                t_pre = time.monotonic()
                try:
                    pre_ok, pre_err = await asyncio.wait_for(
                        self._check_precondition(session, step, merged_vars),
                        timeout=max(1.0, pre_budget_ms / 1000.0),
                    )
                except TimeoutError:
                    pre_ok, pre_err = (
                        False,
                        f"precondition timed out after {pre_budget_ms}ms "
                        "(device call may still be in flight; aborting run)",
                    )
                except Exception as exc:
                    pre_ok, pre_err = False, str(exc)
                outcome.elapsed_ms += int((time.monotonic() - t_pre) * 1000)
                outcome.precondition_passed = pre_ok
                if not pre_ok:
                    outcome.success = False
                    outcome.error = f"precondition failed: {pre_err}"
                    self._record_step(step, args, outcome, None)
                    failed_index = index
                    run_error = outcome.error
                    logger.warning(
                        f"Recipe {recipe.id} aborted at step {index} "
                        f"({step.action}): {outcome.error}"
                    )
                    break

            t0 = time.monotonic()
            try:
                if step.action == "inject_file":
                    # Runner-side staging action: adb push from the device
                    # workspace (see _inject_file); not an MCP tool.
                    res = await asyncio.wait_for(
                        self._inject_file(args, device_serial),
                        timeout=max(1.0, budget_ms / 1000.0),
                    )
                else:
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
            outcome.elapsed_ms += int((time.monotonic() - t0) * 1000)

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
                    # A failed check is evidence, not (by default) a crash —
                    # the action still dispatched, so the run continues unless
                    # the spec declared this assert required.
                    outcome.error = f"assert_after failed: {assert_err}"
                    if (step.assert_after or {}).get("required"):
                        outcome.success = False
            elif step.assert_after is not None:
                outcome.assert_passed = None

            if not outcome.success and step.on_fail:
                repair = await self._attempt_step_repair(
                    session, recipe, index, step, args, outcome, run_id
                )
                if repair is not None:
                    outcome.repaired = True
                    outcome.repair = repair
                    outcome.success = True
                    outcome.elapsed_ms += int(repair.get("elapsed_ms") or 0)
                    if step.assert_after:
                        # The repair changed the screen: re-check once so a
                        # required assert validates the repaired state too.
                        t_assert = time.monotonic()
                        passed, assert_err, shot_name, shot_bytes = (
                            await self._check_assert(
                                session, step, run_id, index, merged_vars
                            )
                        )
                        outcome.elapsed_ms += int(
                            (time.monotonic() - t_assert) * 1000
                        )
                        outcome.assert_passed = passed
                        outcome.post_screenshot_name = shot_name
                        if not passed:
                            outcome.error = (
                                "assert_after still failing after repair:"
                                f" {assert_err}"
                            )
                            if (step.assert_after or {}).get("required"):
                                outcome.success = False

            self._record_step(step, args, outcome, shot_bytes)

            if not outcome.success:
                failed_index = index
                run_error = outcome.error
                logger.warning(
                    f"Recipe {recipe.id} aborted at step {index} ({step.action}): {outcome.error}"
                )
                break

        finished = datetime.now().astimezone()
        result = RecipeRunResult(
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
        if device_serial:
            append_memory(device_serial, memory_line(recipe.name or recipe.id, result))
        return result
