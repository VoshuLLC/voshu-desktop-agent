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

"""Pydantic models for the recipe engine.

A recipe carries canonical wire-level calls: ``action`` is an ``ActionSpec``
tool name (``click``, ``swipe``, ``input_text``, ``press_key``,
``wait_for_delay``, ``wait_for_text``, ``manage_app``, ``open_link``, ...)
and ``args`` are that tool's wire-dialect arguments — the exact pair
``ActionSession.call(action, args)`` dispatches, so replay needs no model
translation. String values in ``args`` may contain ``{{variable}}``
placeholders, substituted per run from ``variables`` (recipe defaults
overridden by the caller's values).
"""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "Recipe",
    "RecipeRunResult",
    "RecipeStep",
    "RecipeStepResult",
    "VARIABLE_PATTERN",
    "is_valid_recipe_id",
]

#: ``{{name}}`` placeholder inside a step argument string.
VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_RECIPE_ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}")


def is_valid_recipe_id(recipe_id: str) -> bool:
    """A recipe id is a filesystem-safe slug (it names the recipe's JSON file)."""
    return bool(recipe_id) and bool(_RECIPE_ID_PATTERN.fullmatch(recipe_id))


class RecipeStep(BaseModel):
    """One replayable device action lifted from a recorded step."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    action: str
    """Canonical ``ActionSpec`` wire tool name."""

    args: dict[str, Any] = Field(default_factory=dict)
    """Wire-dialect arguments; string values may hold ``{{var}}`` placeholders."""

    precondition: dict[str, Any] | None = None
    """Optional pre-action gate through the same actuation channel:
    ``{"type": "launch_app", "package": "<pkg>"}`` foregrounds the app,
    ``{"type": "wait_for_text", "text": "...", "timeout_ms": ...}`` blocks until
    a string is on screen, and ``{"type": "if_text_then_tap", "text": "...",
    "timeout_ms": ...}`` taps the matching element only when it appears — the
    conditional onboarding-dismiss hook; an absent match is never a failure.
    ``package``/``text`` values take ``{{var}}``."""

    assert_after: dict[str, Any] | None = None
    """Optional post-action check, e.g. ``{"type": "text_present", "text": "..."}``
    or ``{"type": "screenshot"}``. ``text`` values also take ``{{var}}``. A
    failed check is recorded (``assert_passed: false``) and the run continues
    unless the spec sets ``"required": true``."""

    on_fail: str | None = None
    """Opt-in fallback when the step's action or ``required`` assert fails.
    ``"vision"`` asks the configured VLM (one bounded call) for a single
    replacement ActionSpec, executes it, and persists the fix into the recipe
    file. ``None`` (the default) keeps replay strictly deterministic — no LLM.
    """

    timeout_ms: int = 15000
    """Per-step budget: bounds the action call and the ``assert_after`` check."""

    description: str | None = None


class Recipe(BaseModel):
    """The deterministic replay form of one recorded session."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    """Filesystem-safe slug; the recipe lives at ``traces/recipes/<id>.json``."""

    name: str
    created: str = ""
    """ISO-8601 creation timestamp."""

    source_session: str | None = None
    device_constraints: dict[str, Any] = Field(default_factory=dict)
    """Source-device facts replay may need (e.g. ``{"device_width": 1080}``)."""

    variables: dict[str, Any] = Field(default_factory=dict)
    """Variable name -> default value; ``run`` variables override these."""

    steps: list[RecipeStep] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    """Recorded actions deliberately left out (failed, non-device, unreplayable)."""

    patches: list[dict[str, Any]] = Field(default_factory=list)
    """Repair log: entries appended when an ``on_fail`` lane rewrote a step in
    place (``{ts, run_id, step_index, kind, old_action, old_args, new_action,
    new_args}``) — the audit trail for self-healing recipes."""

    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_is_slug(cls, value: str) -> str:
        if not is_valid_recipe_id(value):
            raise ValueError(f"recipe id must be a slug, got {value!r}")
        return value


class RecipeStepResult(BaseModel):
    """Per-step outcome inside a ``RecipeRunResult``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    step_index: int
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    """Arguments after variable substitution (what was actually dispatched)."""

    success: bool = False
    error: str | None = None
    elapsed_ms: int = 0
    precondition_passed: bool | None = None
    """``None`` when the step declares no ``precondition``."""
    assert_passed: bool | None = None
    """``None`` when the step declares no ``assert_after``."""

    repaired: bool = False
    """``True`` when an ``on_fail`` fallback recovered this step."""
    repair: dict[str, Any] | None = None
    """What the fallback lane dispatched, e.g. ``{"kind": "vision", "action":
    "click", "args": {...}, "from_error": "..."}``."""

    post_screenshot_name: str | None = None
    """Image record name / shot filename when an assert captured a screenshot."""


class RecipeRunResult(BaseModel):
    """Outcome of one recipe replay — a session-shaped record of the run."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    recipe_id: str
    recipe_name: str = ""
    session_id: str | None = None
    """DataEngine session the run recorded under, when tracing was attached."""

    device_serial: str | None = None
    started_at: str = ""
    finished_at: str = ""
    elapsed_ms: int = 0
    success: bool = False
    failed_step_index: int | None = None
    error: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    """Effective variable set (recipe defaults merged with caller overrides)."""

    steps: list[RecipeStepResult] = Field(default_factory=list)
