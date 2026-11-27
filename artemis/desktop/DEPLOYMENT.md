# winagentd deployment runbook

Windows counterpart of `macagentd.py`: a zero-dependency PowerShell 5.1 daemon
exposing the ARTEMIS desktop HTTP contract (`/health /screenshot /frame
/hierarchy /input /type /key /shell /app`) so `HttpDesktopDriver` can drive a
Windows machine exactly like a macOS one. Input is `SendInput` — kernel-level,
indistinguishable from human input, and it moves the real system cursor, which
is the ghost cursor: anyone watching the console sees the agent work.

## Verified first deployment: VM100 (2026-11-26)

DESKTOP-UHI01BH, 10.1.10.84, Proxmox VM 100. Full contract green: shell exec,
input injection, UIA hierarchy (35 elements in ~2.3s), token auth. Daemon runs
in interactive session 1, auto-restarts at logon via scheduled task.

## Hard-won deployment lessons (all baked into install-winagentd.ps1)

1. **Defender AMSI blocks the daemon.** SendInput injection + hidden listener
   is textbook RAT behavior — of course it gets flagged. The fix is a *scoped*
   `Add-MpPreference -ExclusionPath <daemon dir>` applied BEFORE first run.
   NEVER use `ExclusionProcess` — a machine-wide hole that would strip
   protection from all PowerShell malware on the box. (Briefly made exactly
   that mistake during first deploy; reverted within minutes. Don't repeat it.)
   Long-term answer: a signed compiled agent binary, which doesn't trip AMSI
   the way a raw script does.

2. **The daemon must live in the interactive user session.** qemu guest exec,
   WinRM, and services all land in session 0 — screenshots black, input goes
   nowhere. Deploy as an interactive-token scheduled task
   (`schtasks /ru <user> /it /sc onlogon`): no stored password, fires only
   when the user is logged in, which is exactly when a drivable desktop exists.
   Session 0 execution is acceptable only as a compile/bind smoke test.

3. **Serve payloads over the target's LAN, not tailnet.** VM guests may have
   no tailscale client even when they reach tailnet-adjacent services. First
   deploy failed silently on exactly this: zero requests ever hit the server —
   the server log is ground truth, check it first.

4. **ASCII-only script content.** PowerShell 5.1 reads UTF-8-without-BOM as
   ANSI; multibyte punctuation (em-dashes, arrows) mangles. Harmless inside
   comments, lethal inside strings. Keep the daemon ASCII-clean; verify with
   the engine's own parser on the target before installing
   (`Parser::ParseFile` — the installer does this automatically and aborts on
   error).

5. **PowerShell 5.1 ≠ pwsh 7 syntax.** No `?:` ternary, no `??`. Static method
   calls need parens and commas: `[W32]::ClickAt($x, $y, "left", 1, 50)`.
   Parse-verify on 5.1 — a pwsh 7 parse pass proves nothing.

6. **Encoding for remote exec.** When pushing commands through qemu guest
   exec or similar nested channels, use `powershell -EncodedCommand` with
   UTF-16LE base64 — it is the only quoting-safe transport across
   ssh→bash→qm→powershell layering.

## Install (VM100-style, from the Proxmox host)

```
# payload server on the LAN (any static file server; check its access log)
cd /root/pubserve && python3 -m http.server 8077 &

# on the guest, as SYSTEM/admin:
powershell -ep bypass -c "iex (iwr -useb http://<lan-ip>:8077/install-winagentd.ps1).content"
```

Installer is idempotent and self-verifying: exits 0 only when the listener
answers, and writes a step-by-step `install.log` next to the daemon.

## Enrolling in the ARTEMIS fleet

`enroll(kind="win", serial, host, port, token)` — see `artemis/fleet/agentd.py`.
Reachability note: the control machine must route to the guest. When the guest
LAN is unreachable, either install tailscale in the guest template or run a
forwarder on the hypervisor (`socat TCP-LISTEN:8177,fork TCP:<guest>:8177`)
and enroll against the forwarder.

## Security posture

- Token auth on every route except `/health`; token is machine-local at
  `C:\ProgramData\winagentd\token.txt`, never logged or committed.
- The firewall rule opens the port LAN-wide; the token is the auth boundary.
  Owner-phase acceptable; customer-grade requires per-session credentials.
- The daemon is a full-control channel by design. Treat its port like root SSH.

## v2 roadmap (designed 2026-11-26, partially shipped)

SHIPPED: `/invoke` — semantic actuation. POST {name, role?, exact?} finds an
element by name in the UIA tree and fires its control pattern
(Invoke > Toggle > coordinate-click fallback using element bounds). Verified
live: a deep Edge button invoked by name — and its post-action rename proved
the actuation fired. This is the primary actuation mode going forward;
coordinate input is the fallback for canvas-like surfaces.

NEXT (design final, implementation pending):
1. `/stream` — SSE channel pushing UIA StructureChanged/property diffs so
   agents hold a live shadow tree instead of polling. Verification becomes
   passive: assertions arrive as stream events.
2. CDP bridge — Edge launches with --remote-debugging-port pinned in the VM
   image; daemon merges Chromium's unvirtualized Accessibility.getFullAXTree
   (no UIA virtualization limits — the Amazon-results gap dies at the root)
   into the same element namespace as UIA native chrome. Implementation path:
   System.Net.WebSockets.ClientWebSocket inside the daemon's existing
   Add-Type block; resolve element coordinates lazily via DOM.getBoxModel
   only for actionable candidates.
3. Driver shadow-model — HttpDesktopDriver keeps a diff-applied live tree;
   get_screen_data becomes cheap. Playbook anchors bind AutomationId+role
   +name with coordinate fallback — capture/replay becomes possible.

## CDP bridge deployment state (2026-11-27 — mid-transport handoff)

The CDP build (commit cdf3f7b) is committed but NOT yet running on VM100.
Transport war notes for the next session:

1. File servers: port 80 on pve-leh is a node service, NOT apache. There is no
   reliable HTTP file server. Do not waste time on /var/www/html.
2. The chunked-base64 push through the daemon's own /shell is the working
   channel (wa-push.py on pve-leh), BUT one chunk of the last push was mangled
   in transit (target file 33504 bytes vs source 35604 — exactly one 2800-char
   chunk short, likely a base64 `+` eaten by a quoting layer). FIX: re-push
   with per-chunk verification — after each Add-Content, read back the file
   length and retry the chunk if it didn't grow by exactly len(chunk).
3. Bootstrap decode chain that works: push decode script as base64 in d.b64
   (small), certutil-decode it (fine for small files), run it — it decodes
   wa.b64 with PowerShell [Convert]::FromBase64String into winagentd.ps1.
   certutil FAILS on the big file directly (single-line base64 too long).
4. VM state right now: wa.b64 + d.b64 + dec.ps1 all staged in
   C:\ProgramData\winagentd\; daemon still running OLD build (0.1.0, healthy).
   Edge may need relaunch with: --remote-debugging-port=9333
   --user-data-dir=C:\ProgramData\winagentd\edge-profile --no-first-run
   (dedicated automation profile — modern chromium refuses debug port on the
   default profile). Amazon guest cart does not carry over; acceptable.
5. After re-push: schtasks end+run, verify /hierarchy response contains
   "cdp":{"ok":true} and elements with source=cdp + href fields on an Amazon
   results page. Then the acceptance test: product element visible in tree ->
   invoke by name -> cart count increments.

Better long-term transport (implement once, ends this class of problem):
add a /deploy route to the daemon that accepts raw base64 in the POST body
of a single request — no chunking, no shell quoting, no intermediate files.
