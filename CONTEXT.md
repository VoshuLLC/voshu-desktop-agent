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
- `artemis/recipes/workspaces.py` — per-device durable workspaces
  (`traces/workspaces/<serial>/{notes.md,assets/,memories.md,coords.json}`);
  `@workspace:<serial>/<path>` arg resolution + `inject_file` step (adb push)
  + `memory_pack()` planner API; `GET /api/fleet/devices/{id}/memory` exposes it.
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

- 2026-11-07 ~07:15 ET — White-label layer "Voshu Device Control" (uncommitted,
  devin): `apps/admin_console/BRAND.json` {name, short, tagline, accent} +
  `routers/brand.py` `GET /api/brand` (60s cache, defaults fallback, verified
  200 in-process). Visible strings swapped in `index.html` (title + fleet
  brand), `fleet.html` (title + brand), showcase src + live `dist/` bundle
  (title + 6 wizard strings — dist is gitignored, served per-request so it is
  already live), server.py FastAPI title/fallback/argparse, CLI `main.py` +
  `commands/ui.py` help/banner strings → "Voshu Device Control (Artemis
  engine)". Fixed latent bug: try-branch router import in server.py was
  missing `recipes` (NameError path if that branch ever fully ran). Identity
  kept: package/module/route paths, `artemis` command, `artemis ui`, helper
  APK, ARTEMIS_* env vars, UA string. NOT rebranded: doctor/init/helper/batch
  command internals, code comments/docstrings referencing the Artemis engine.
- 2026-11-07 ~06:30 ET — Desktop fleet member `kind:"mac"` shipped (uncommitted,
  devin): `artemis/desktop/macagentd.py` — stdlib-only HTTP agent on
  127.0.0.1:7778 (system python3, zero pip deps): `screencapture`→PNG/JPEG +
  MJPEG `/stream`, CGEvent input via a ctypes Quartz bridge (click/double/
  right/long/swipe/scroll), unicode `/type`, `/key` (ENTER/TAB/ESC/BACK→esc/
  HOME→⌘-space/arrows/F-keys; POWER→501, VOLUME via osascript), metachar-
  blocked `/shell`, `/app` launch|terminate, `/doctor` TCC probe (real
  `AXIsProcessTrustedWithOptions`/`CGPreflightScreenCaptureAccess`), and a
  working ctypes `/hierarchy` (254-node frontmost-app AX tree emitted as
  uiautomator-shaped dicts). Bearer token optional via `MACAGENTD_TOKEN`.
  `artemis/fleet/agentd.py` — `enroll()` + `sync_agents()` + httpx forwarding;
  `POST /api/fleet/enroll` added; input/key/shell/frame/stream forward to
  `device.host` for agent kinds (same response shapes); run/reboot → honest
  501; DELETE → unenroll. `FleetDevice.host` added; `sync_adb` skips mac|win.
  Verified live on a :8001 test console: enrolled `mac-studio` → status free,
  frame/shell/input/key/MJPEG-stream all real through `/api/fleet/devices/…`;
  one center click fired (landed inside the QEMU guest window). TCC grants
  already held by the parent chain — no prompt appeared. `macagentd` left
  RUNNING, registered as `artemis-macagentd` in `~/local-services/
  services.json` + SERVICES.md #16.
- 2026-11-07 ~06:20 ET — Recipes v1.2 (uncommitted, devin): `{{var}}` values +
  step args accept `@workspace` file refs resolved to per-device workspaces;
  new `inject_file` step action (`{workspace_path, device_path}` → adb push);
  `POST /api/recipes/{id}/run` takes `device_serial:"auto"` (first free
  avd/emulator in the fleet registry) or `"all"` (one queued run per free
  device, `runs`+`tasks` in response); fleet devices claimed busy on dispatch
  and freed by the run watcher; `RecipeStep.on_fail:"vision"` opt-in lane does
  ONE bounded VLM call (`validator_pixel_safety_net` node → Qwen3-VL) for a
  replacement ActionSpec, marks the step `repaired`, and persists the fix into
  the recipe file (`recipe.patches` audit log). Verified live on :8000 daemon
  (worker picks up new code per-run): `inject_file` pushed a workspace asset
  to emulator-5556:/sdcard/Download + memories.md line written; auto/all and
  repair paths unit-verified in-process; router-level auto/all needs daemon
  restart to serve.
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

- White-label: `/api/brand` route + FastAPI title + CLI help land on next
  daemon restart; HTML/dist branding already live (files read per request).
  Deeper CLI surface (doctor/init/helper banners) still says Artemis — open
  scope if full rebrand wanted.
- Recipes v1.1+v1.2 landed (uncommitted diff): precondition steps + required-
  flagged asserts + `PUT /api/recipes/{id}` + console UI + workspaces/inject/
  auto-all/vision-repair. Daemon restart needed for the new routes + `auto`/
  `all` selector to be live (workers already pick up the new runner per-run).
  NOTE: the same uncommitted diff also carries the `kind:"mac"` desktop work
  (done ~06:30, entry above) — `artemis/fleet/{registry,provisioner,agentd}.py`,
  `routers/fleet.py`, `artemis/desktop/`. One restart serves both.
- Multi-goal `/api/run` batch; Compute `devices` registry contract (needs doc in
  voshu-compute `docs/architecture/` before any code crosses that boundary).
- Desktop `kind:"mac"` SHIPPED (above); `kind:"win"` winagentd still open —
  spec `voshu-docs/05-design/VOSHU-DEVICE-CONTROL.md`. TCC grants held on this
  box; other macs still need manual Accessibility + Screen Recording toggles.
- mac `/run` (LLM sessions on the desktop) needs HttpDesktopDriver +
  DesktopActuator per spec §A.5 — returns 501 today.
- Golden re-baseline flow: bump `GOLDEN_REV` → drain/reclone fleet.

## 2026-11-08 (devin — href/embed link audit, read-only)

Audited every href/src/iframe/window.open across the served surfaces
(`apps/admin_console/{index,fleet}.html`, `apps/showcase_ui` src + built
`artemis/resources/showcase_ui`). Findings:
- Admin console (`/admin`, `/fleet`): fully self-contained — all links/media
  are same-origin (`/api/...`, `/videos/...`, icons). No CDNs, no iframes;
  device screens are MJPEG `<img>`. Only nit: `/api/traces/<id>/download`
  `target=_blank` lacks `rel=noopener` (same-origin, low risk).
- Showcase UI (upstream Angular app): EXTERNAL embeds — Google Fonts
  (`fonts.googleapis.com` preconnect+stylesheets, built bundle also inlines
  `fonts.gstatic.com` woff2) and two doc links on the home page:
  `aistudio.google.com/app/apikey` (upstream Google branding leftover on a
  Voshu-labeled surface) + `developer.android.com/studio` (legit).
- `floating-video-player.component.ts` L467/615 `window.open(url,'_blank')`
  (internal replay URLs; no noreferrer features — low risk).
- `playground/` cuttlefish iframe = dev-only, not part of the served surface.
- Server sets `X-Frame-Options: deny` (live-verified via mission embeds
  probe) — Ops embeds refuse honestly.
