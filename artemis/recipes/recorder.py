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

"""Session -> recipe extraction.

``RecipeRecorder.from_session`` mines a finished session's stored steps
(``StepRecord.action_taken`` + ``last_execution_result``) into the recipe's
ordered wire calls. Two recorded shapes are supported:

* **Flash** — ``action_taken`` is a single dict whose ``action`` is already a
  canonical ``ActionSpec`` name and whose ``args`` are the agent dialect; the
  resolved ``normalized_coordinates`` on the record is the point of truth
  (an element-index target replays through the coordinate the executor
  resolved, never the index).
* **Pro** — ``action_taken`` is the Operator's structured-decision list
  (``tap``/``long_press_on``/``focus_and_input_text``/... verbs); each item is
  lowered to the wire with the same ``to_canonical_call`` the Validator used.

Failed actions stay out of ``steps`` and land in ``Recipe.skipped`` with the
reason they were dropped.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from artemis.config.paths import DB_PATH, TRACES_PATH
from artemis.data_engine.history_reader import OfflineHistoryReader
from artemis.data_engine.models import StepRecord
from artemis.mcp.action_manifest import DEVICE_ACTIONS, INTERNAL_ACTIONS
from artemis.mcp.action_names import OPERATOR_ACTION_TO_CANONICAL, to_canonical_call
from artemis.recipes.models import Recipe, RecipeStep, is_valid_recipe_id
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "RECIPES_DIR",
    "RECIPE_RUNS_DIR",
    "RecipeRecorder",
    "delete_recipe",
    "list_recipes",
    "load_recipe",
    "recipe_path",
]

#: Recipe JSON files live next to the session traces they were mined from.
RECIPES_DIR = Path(TRACES_PATH) / "recipes"
#: One JSON per finished recipe run, written by the daemon-side run worker.
RECIPE_RUNS_DIR = RECIPES_DIR / "runs"

#: Action names that record session intent rather than a device interaction —
#: they can never be replayed through the action session.
_NON_ACTIONS = {"report_task_status", "ask_user", "done", "finish"}

_KNOWN_BARE_KEYS = {"home", "back", "enter", "delete", "tab", "search", "menu", "app_switch"}


def recipe_path(recipe_id: str, root: Path | None = None) -> Path:
    if not is_valid_recipe_id(recipe_id):
        raise ValueError(f"invalid recipe id: {recipe_id!r}")
    return (Path(root) if root else RECIPES_DIR) / f"{recipe_id}.json"


def load_recipe(recipe_id: str, root: Path | None = None) -> Recipe:
    path = recipe_path(recipe_id, root)
    if not path.is_file():
        raise FileNotFoundError(f"recipe not found: {recipe_id}")
    return Recipe.model_validate(json.loads(path.read_text(encoding="utf-8")))


def list_recipes(root: Path | None = None) -> list[Recipe]:
    base = Path(root) if root else RECIPES_DIR
    if not base.is_dir():
        return []
    recipes = []
    for path in sorted(base.glob("*.json")):
        try:
            recipes.append(Recipe.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        except (ValueError, OSError) as exc:
            logger.warning(f"Skipping unreadable recipe file {path}: {exc}")
    return recipes


def delete_recipe(recipe_id: str, root: Path | None = None) -> bool:
    path = recipe_path(recipe_id, root)
    if not path.is_file():
        return False
    path.unlink()
    return True


def _point(value: Any) -> list[int] | None:
    """A normalized [x, y] pair, or None for indices/other shapes."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [int(value[0]), int(value[1])]
        except (TypeError, ValueError):
            return None
    return None


def _wire_args_from_agent_call(item: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Lowers a Flash recorded action (canonical name + agent args) to the wire.

    Returns ``(name, wire_args)``, or ``None`` when the record lacks what the
    wire dialect needs (e.g. an element-index target with no resolved
    coordinate — an index means nothing without the session's element list).
    """
    action = str(item.get("action") or "")
    args = dict(item.get("args") or {})
    norm = item.get("normalized_coordinates")
    target = _point(norm) or _point(args.get("target"))

    if action == "click":
        if target is None:
            return None
        return "click", {
            "target": target,
            "times": int(args.get("times") or 1),
            "delay_ms": int(args.get("delay_ms") or 100),
        }
    if action == "long_press":
        if target is None:
            return None
        return "long_press", {
            "target": target,
            "duration_ms": int(args.get("duration") or args.get("duration_ms") or 1000),
        }
    if action == "input_text":
        wire: dict[str, Any] = {
            "text": str(args.get("text", "")),
            "clear_exist": bool(args.get("clear_exist", True)),
        }
        if target is not None:
            wire["target"] = target
        return "input_text", wire
    if action == "focus_and_clear_text":
        if target is None:
            return None
        return "focus_and_clear_text", {"target": target}
    if action == "erase_one_char":
        return "erase_one_char", {}
    if action == "click_sequence":
        sequence = args.get("sequence")
        if not sequence:
            return None
        return "click_sequence", {
            "sequence": sequence,
            "delay_ms": int(args.get("delay_ms") or 50),
        }
    if action == "swipe":
        start = _point(item.get("normalized_start_coordinates")) or _point(args.get("start"))
        end = _point(item.get("normalized_end_coordinates")) or _point(args.get("end"))
        if (start is None or end is None) and isinstance(norm, (list, tuple)) and len(norm) == 4:
            start, end = [int(norm[0]), int(norm[1])], [int(norm[2]), int(norm[3])]
        if start is None or end is None:
            return None
        return "swipe", {
            "start": start,
            "end": end,
            "duration_ms": int(args.get("duration") or args.get("duration_ms") or 800),
        }
    if action == "press_key":
        key = str(args.get("key") or args.get("keycode") or "")
        bare = key.removeprefix("KEYCODE_").lower() if key else ""
        return "press_key", {"key": bare if bare in _KNOWN_BARE_KEYS else key}
    if action == "manage_app":
        return "manage_app", {
            "action": str(args.get("action", "")),
            "app_name": str(args.get("app_name", "")),
        }
    if action == "wait_for_delay":
        return "wait_for_delay", {
            "time_in_ms": int(args.get("time_in_ms") or args.get("duration") or 0)
        }
    if action == "wait_for_text":
        return "wait_for_text", {
            "text": str(args.get("text", "")),
            "wait_state": str(args.get("wait_state") or "appear"),
            "timeout_ms": int(args.get("timeout_ms") or 5000),
        }
    if action == "open_link":
        return "open_link", {"url": str(args.get("url", ""))}
    return None


def _wire_call(item: dict[str, Any], dims: tuple[int, int]) -> tuple[str, dict[str, Any]] | None:
    """One recorded action dict -> ``(canonical_name, wire_args)``, or None."""
    action = str(item.get("action") or "")
    if action in OPERATOR_ACTION_TO_CANONICAL:
        try:
            return to_canonical_call(item, dims)
        except ValueError:
            return None
    if action in DEVICE_ACTIONS - INTERNAL_ACTIONS:
        return _wire_args_from_agent_call(item)
    return None


def _ran(item: dict[str, Any]) -> bool:
    """False for burst entries that never dispatched (Pro 'Skipped' markers)."""
    attempts = item.get("attempts")
    if isinstance(attempts, list):
        return not any(str(a).startswith("Skipped") for a in attempts)
    return True


def _successful_items(step: StepRecord) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Splits a step's recorded actions into (executed, dropped) item lists.

    ``last_execution_result`` is the arbiter: the Validator's report carries
    the attempted ``execution`` list (a failure stops the loop, so the last
    attempted entry is the failed one); the Flash record carries only
    ``status`` so a failed step drops its whole action.
    """
    taken = step.action_taken
    if isinstance(taken, str):
        try:
            taken = json.loads(taken)
        except (ValueError, TypeError):
            taken = None
    items = [taken] if isinstance(taken, dict) else list(taken) if isinstance(taken, list) else []
    if not items:
        return [], []

    result = step.last_execution_result if isinstance(step.last_execution_result, dict) else {}
    status = str(result.get("status") or "").lower()

    execution = result.get("execution")
    if isinstance(execution, list) and execution:
        ran = [dict(e) for e in execution if isinstance(e, dict) and _ran(e)]
        dropped: list[dict[str, Any]] = []
        if status == "failed" and ran:
            dropped.append(ran.pop())
        return ran, dropped

    if status == "failed":
        return [], [dict(i) for i in items if isinstance(i, dict)]
    return [dict(i) for i in items if isinstance(i, dict)], []


def _inject_params(value: Any, param_hints: dict[str, Any], variables: dict[str, Any]) -> Any:
    """Replaces hinted literal values in string args with ``{{name}}``."""
    if isinstance(value, dict):
        return {k: _inject_params(v, param_hints, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_inject_params(v, param_hints, variables) for v in value]
    if not isinstance(value, str) or not param_hints:
        return value
    for name, hint in param_hints.items():
        literal = str(hint)
        if literal and literal in value:
            variables.setdefault(name, hint)
            value = value.replace(literal, "{{" + name + "}}")
    return value


def _describe(item: dict[str, Any]) -> str | None:
    args = item.get("args") if isinstance(item.get("args"), dict) else {}
    desc = item.get("target_description") or args.get("target_description")
    return str(desc) if desc else None


class RecipeRecorder:
    """Builds a :class:`Recipe` from a recorded session's stored history."""

    def _steps_from_files(self, session_id: str) -> list[StepRecord]:
        """Step-file fallback when the DB has no rows for this session.

        Covers compiled replay artifacts: ``traces/<sid>/steps/step_*/step.json``
        and ``traces/replay/data/<sid>_chunked/step_*/step.json``.
        """
        candidates = [
            Path(TRACES_PATH) / session_id / "steps",
            Path(TRACES_PATH) / "replay" / "data" / f"{session_id}_chunked",
        ]
        records: list[StepRecord] = []
        for base in candidates:
            if not base.is_dir():
                continue
            for step_file in sorted(base.glob("step_*/step.json")):
                try:
                    records.append(StepRecord.model_validate(json.loads(step_file.read_text())))
                except (ValueError, OSError) as exc:
                    logger.warning(f"Skipping unreadable step file {step_file}: {exc}")
        records.sort(key=lambda s: s.step_number or 0)
        return records

    def _session_steps(self, session_id: str) -> tuple[list[StepRecord], dict[str, Any]]:
        reader = OfflineHistoryReader(DB_PATH, TRACES_PATH, session_id)
        try:
            steps = reader.storage.get_steps(reader.session_id) or []
        except Exception as exc:
            logger.warning(f"DB step read failed for {session_id}: {exc}")
            steps = []
        device_info: dict[str, Any] = {}
        try:
            session = reader.storage.get_session(reader.session_id)
            device_info = dict(session.device_info) if session else {}
        except Exception as exc:
            logger.debug(f"Session metadata unavailable for {session_id}: {exc}")
        if not steps:
            steps = self._steps_from_files(session_id)
        if not steps:
            raise FileNotFoundError(f"no recorded steps found for session {session_id}")
        return steps, device_info

    def from_session(
        self,
        session_id: str,
        recipe_id: str,
        param_hints: dict[str, Any] | None = None,
        name: str | None = None,
        tags: list[str] | None = None,
    ) -> Recipe:
        """Extracts the successful device actions of ``session_id`` into a recipe."""
        session_id = str(session_id)
        steps, device_info = self._session_steps(session_id)

        width = int(device_info.get("device_width") or 1080)
        height = int(device_info.get("device_height") or 2400)
        dims = (width, height)

        variables: dict[str, Any] = {}
        recipe_steps: list[RecipeStep] = []
        skipped: list[dict[str, Any]] = []

        def _drop(step_number: int, item: dict[str, Any], reason: str) -> None:
            skipped.append(
                {
                    "step_number": step_number,
                    "action": item.get("action"),
                    "reason": reason,
                }
            )

        for step in steps:
            executed, dropped = _successful_items(step)
            for item in dropped:
                _drop(step.step_number, item, "action failed during source run")
            for item in executed:
                action = str(item.get("action") or "")
                if action in _NON_ACTIONS:
                    _drop(step.step_number, item, "not a device action")
                    continue
                call = _wire_call(item, dims)
                if call is None:
                    _drop(step.step_number, item, "unreplayable (missing resolved coordinates or args)")
                    continue
                name_, wire_args = call
                wire_args = _inject_params(wire_args, param_hints or {}, variables)
                recipe_steps.append(
                    RecipeStep(
                        action=name_,
                        args=wire_args,
                        description=_describe(item),
                    )
                )

        device_constraints = {
            k: device_info[k]
            for k in ("device_width", "device_height", "device_id", "mobile_platform")
            if device_info.get(k) is not None
        }

        return Recipe(
            id=recipe_id,
            name=name or recipe_id,
            created=datetime.now().astimezone().isoformat(timespec="seconds"),
            source_session=session_id,
            device_constraints=device_constraints,
            variables=variables,
            steps=recipe_steps,
            skipped=skipped,
            tags=list(tags or []),
        )

    def save(self, recipe: Recipe, root: Path | None = None) -> Path:
        """Writes ``traces/recipes/<recipe.id>.json`` (atomically)."""
        path = recipe_path(recipe.id, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(recipe.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        logger.info(f"Saved recipe {recipe.id} ({len(recipe.steps)} steps) to {path}")
        return path
