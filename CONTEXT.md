# Artemis — project context

Fork of `google/artemis` (mobile device agent automation, Apache-2.0). Voshu-operated
as the device-control plane: phone-farm fleet + deterministic recipe engine + Voshu
Router inference lanes. Console: `artemis ui` → `http://127.0.0.1:8000` (`/admin`
unified workspace, `/fleet` standalone wall).

## Current state (2026-11-07)

- `artemis/fleet/` — golden AVD `voshu-gold` (`/Volumes/DevData/Android/avd/voshu-farm`,
  rev-20261107, Pixel6/A34/arm64, helper APK + wizard baked). Recyclable clones via
  `POST /api/fleet/provision`; registry+stream+provisioner; per-serial MJPEG.
- `artemis/recipes/` — record session action traces → `traces/recipes/<id>.json`;
  replay via `artemis.recipes.worker` as queue-managed session, zero LLM calls;
  `{{var}}` injection + preconditions (`launch_app`/`wait_for_text`/
  `if_text_then_tap`) + `required`-flagged asserts (v1.1 landed 2026-11-07).
- `routers/`: `fleet.py`, `recipes.py` mounted in `apps/admin_console/server.py`.
- `index.html` = view-switch rail (Fleet | Sessions) on Artemis light tokens;
  Select-mode → broadcast goal dispatch across devices.
- LLM: all nodes resolve through `OPENAI_BASE_URL=http://voshu-router:8787/v1`.
  Vision → `ollama/mlx-community/Qwen3-VL-8B-Instruct-4bit` (router lane → enrolled
  Mac Studio `DevOps's Mac Studio` @ `http://100.91.79.7:18092`, engine
  `llama-server`; "ollama/" is a legacy label for a generic-OpenAI upstream lane).
  planner→`agent/planner`, utils→`agent/agy-flash` (NIM step-5 / Copilot lanes).
  pollinations/duck/cf trimmed from those agents' `allows` (payload-incompatible).
- scrcpy: patched 5.0.1 at `~/.local/scrcpy/bin/scrcpy` (recorder ctx-init race fix,
  10/10 stress passes); `run-voshu.sh` sets `ARTEMIS_SCRCPY_PATH` + PATH precedence.
- Old AVDs `halo34`/`PCF_Pixel_7` retired to `/Volumes/DevData/Android/avd/.retired-*`.

## What was done

- 2026-11-07 ~05:55 ET — Recipes v1.1 (uncommitted): `precondition` gate on
  `RecipeStep` evaluated pre-action through the same session channel;
  `if_text_then_tap` polls hierarchy → normalized `click` (conditional
  onboarding dismiss, absence never fails); assert failures now only abort
  when `"required": true`. `from_session(app_package=…)` prepends a
  launch+settle step; `PUT /api/recipes/{id}` hand-edits (validated body);
  console fleet panel gained a Recipes run section + 📼 Record button in the
  session header. Verified on emulator-5556: `pm clear` Maps → recipe
  `maps-skip-onboarding` launched Maps, tapped SKIP at [815,84]
  (`if_text_then_tap dismissed`), screenshot assert passed, 10.4s zero-LLM.
- Commit `42a7b17` (local, unpushed): fleet workspace + recipe engine + provider-
  aware `get_named_llm` + api_base field + string-aware JSONC parser + fence-
  tolerant structured-output fallback for self-managed servers.
- Verified end-to-end: Maps task `ccad88f7` PASS via router+VLM lane; recipe
  `maps-search` replayed on emulator-5556 in 3.05s zero-LLM; two emu clones
  live-provisioned/booted/streaming.

## Open threads

- Recipes v1.1 landed (uncommitted diff): precondition steps + required-flagged
  asserts + `PUT /api/recipes/{id}` + console UI. Daemon restart needed for the
  new route + `app_package` field to be live (workers already pick up the new
  runner per-run).
- Multi-goal `/api/run` batch; Compute `devices` registry contract (needs doc in
  voshu-compute `docs/architecture/` before any code crosses that boundary).
- Desktop drivers `kind:"mac"|"win"` — spec `voshu-docs/05-design/
  VOSHU-DEVICE-CONTROL.md`; TCC grants are the hard manual step on macOS.
- Golden re-baseline flow: bump `GOLDEN_REV` → drain/reclone fleet.
