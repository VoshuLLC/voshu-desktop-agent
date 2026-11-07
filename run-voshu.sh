#!/usr/bin/env bash
# ARTEMIS on the Voshu Router — env-driven, no .env file, no stored secrets.
# The router key is read from the file at launch and exported to the child
# process only; the key file itself is never opened into this script.
set -euo pipefail
cd "$(dirname "$0")"

export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://voshu-router:8787/v1}"
export OPENAI_API_KEY="$(cat "${VOSHU_ROUTER_KEY_FILE:-$HOME/.secrets/voshu-router-key}")"

# Patched scrcpy 5.0.1 (race fix: demuxer packet-sink open waits for the
# recorder thread to publish the muxer ctx — upstream SIGSEGVs under device
# activity). Built from the v5.0.1 tag into ~/.local/scrcpy.
# ARTEMIS_SCRCPY_PATH is tier-1 in the toolchain resolver; the PATH prepend
# covers the literal "scrcpy" exec in the recording supervisor.
if [ -x "$HOME/.local/scrcpy/bin/scrcpy" ]; then
    export ARTEMIS_SCRCPY_PATH="$HOME/.local/scrcpy/bin/scrcpy"
    export PATH="$HOME/.local/bin:$PATH"
fi

# Model defaults for the harness: planner-grade for Pro runs, flash for
# routine loops. Override per run with ARTEMIS_MODEL / --profile.
export ARTEMIS_MODEL="${ARTEMIS_MODEL:-agent/planner}"

exec ./start.sh "$@"
