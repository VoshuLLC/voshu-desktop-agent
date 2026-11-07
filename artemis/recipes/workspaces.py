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

"""Per-device durable workspaces.

Every device that runs recipes gets a persistent directory under
``traces/workspaces/<serial-slug>/``:

    notes.md      free-form operator notes (same shape as session notes)
    assets/       files a recipe run can push or reference (images, payloads)
    memories.md   append-only run log — one line per finished recipe run
    coords.json   remembered named coordinates/handles (planner seed data)

The runner ensures the workspace before each run and appends one memory line
after it; ``memory_pack`` is the read API a planner calls to preload durable
device context without walking the traces tree.

Step arguments and variable values can point at workspace files through the
``@workspace`` scheme — resolved by :func:`resolve_workspace_path`:

* ``@workspace:<serial>/<path>`` — inside that device's workspace,
* ``@workspace/<path>``        — inside the running device's workspace,
* a bare or relative path      — inside the running device's workspace
  (``assets/`` is preferred for bare filenames),
* an absolute path under ``traces/workspaces/`` passes through unchanged.

``..`` escapes outside the workspaces root are rejected.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from artemis.config.paths import TRACES_PATH
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "WORKSPACES_DIR",
    "WORKSPACE_SCHEME",
    "append_memory",
    "ensure_workspace",
    "is_workspace_ref",
    "memory_pack",
    "resolve_workspace_path",
    "workspace_dir",
]

#: Durable per-device state lives next to session traces.
WORKSPACES_DIR = Path(TRACES_PATH) / "workspaces"

#: Argument prefix marking a value as a workspace file reference.
WORKSPACE_SCHEME = "@workspace"

_SERIAL_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _slug(serial: str) -> str:
    """Filesystem-safe workspace dir name for an adb serial.

    Mirrors the fleet provisioner's id sanitization so ``emulator-5556`` and
    ``host:5555`` get stable, collision-free directory names.
    """
    slug = _SERIAL_SLUG_RE.sub("-", str(serial)).strip("-").lower()
    return slug or "unknown"


def workspace_dir(serial: str) -> Path:
    """The workspace directory for ``serial`` (not necessarily created)."""
    return WORKSPACES_DIR / _slug(serial)


def ensure_workspace(serial: str) -> Path:
    """Creates the workspace skeleton for ``serial``; returns its path."""
    root = workspace_dir(serial)
    try:
        (root / "assets").mkdir(parents=True, exist_ok=True)
        (root / "notes.md").touch(exist_ok=True)
        (root / "memories.md").touch(exist_ok=True)
        coords = root / "coords.json"
        if not coords.exists():
            coords.write_text("{}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Could not ensure device workspace {root}: {exc}")
    return root


def _resolve_under(base: Path, rel: str) -> Path:
    """Workspace-relative resolution; bare filenames prefer ``assets/``."""
    if "/" not in rel and (base / "assets" / rel).exists():
        return (base / "assets" / rel).resolve()
    return (base / rel).resolve()


def is_workspace_ref(value: Any) -> bool:
    """True for string values addressed by the ``@workspace`` scheme."""
    return isinstance(value, str) and (
        value == WORKSPACE_SCHEME
        or value.startswith(f"{WORKSPACE_SCHEME}:")
        or value.startswith(f"{WORKSPACE_SCHEME}/")
    )


def resolve_workspace_path(
    serial: str,
    value: str,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolves a workspace reference to an absolute local path.

    ``serial`` is the running device's serial — it backs the no-serial forms
    (``@workspace/…`` and plain relative paths). Raises ``ValueError`` on a
    ``..`` escape outside the workspaces root and ``FileNotFoundError`` when
    ``must_exist`` and the file is missing.
    """
    raw = str(value).strip()
    root_real = WORKSPACES_DIR.resolve()

    if raw.startswith(WORKSPACE_SCHEME):
        rest = raw[len(WORKSPACE_SCHEME):]
        if rest.startswith(":"):
            rest = rest[1:]
            # ``@workspace:<serial>/<path>`` or bare ``@workspace:<serial>``.
            head, _, tail = rest.partition("/")
            target_serial, rel = (head, tail) if head else (serial, tail)
            if not rel:
                rel = ""
        elif rest.startswith("/"):
            target_serial, rel = serial, rest[1:]
        else:
            raise ValueError(f"malformed workspace reference: {value!r}")
        base = workspace_dir(target_serial)
        resolved = _resolve_under(base, rel) if rel else base.resolve()
    else:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = _resolve_under(workspace_dir(serial), raw)

    try:
        resolved.relative_to(root_real)
    except ValueError:
        raise ValueError(
            f"workspace path escapes the workspaces root: {value!r}"
        ) from None

    if must_exist and not resolved.exists():
        raise FileNotFoundError(
            f"workspace file not found: {value!r} (resolved to {resolved})"
        )
    return resolved


def append_memory(serial: str, line: str) -> None:
    """Appends one line to the device's ``memories.md`` run log."""
    try:
        root = ensure_workspace(serial)
        with open(root / "memories.md", "a", encoding="utf-8") as f:
            f.write(str(line).rstrip("\n") + "\n")
    except OSError as exc:
        logger.warning(f"Could not append workspace memory for {serial}: {exc}")


def memory_pack(serial: str, last_n: int = 20) -> dict[str, Any]:
    """Durable device context for a planner: notes + last ``last_n`` memories.

    Missing pieces read as empty (a workspace is created lazily on first run),
    so the pack is safe to fetch for a device that has never run a recipe.
    """
    root = workspace_dir(serial)

    def _read(name: str) -> str:
        try:
            return (root / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    memories = [ln for ln in _read("memories.md").splitlines() if ln.strip()]
    if last_n > 0:
        memories = memories[-last_n:]
    try:
        coords = json.loads(_read("coords.json") or "{}")
    except ValueError:
        coords = {}
    return {
        "serial": serial,
        "workspace": str(root),
        "notes": _read("notes.md"),
        "memories": memories,
        "coords": coords if isinstance(coords, dict) else {},
    }


def memory_line(recipe_name: str, result: Any) -> str:
    """One memories.md entry for a finished run (recipe, verdict, failed step)."""
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    verdict = "PASS" if getattr(result, "success", False) else "FAIL"
    line = f"{ts} recipe '{recipe_name}' {verdict}"
    failed = getattr(result, "failed_step_index", None)
    if failed is not None:
        step = (getattr(result, "steps", None) or [None] * (failed + 1))[failed]
        action = getattr(step, "action", "?") if step else "?"
        line += f" at step {failed} ({action})"
        error = getattr(step, "error", None) or getattr(result, "error", None)
        if error:
            line += f": {str(error)[:160]}"
    repaired = [
        s.step_index
        for s in (getattr(result, "steps", None) or [])
        if getattr(s, "repaired", False)
    ]
    if repaired:
        line += f" [vision-repaired steps: {repaired}]"
    return line
