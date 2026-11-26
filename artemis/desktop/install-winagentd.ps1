# install-winagentd.ps1 -- idempotent winagentd installer for Windows hosts.
#
# Lessons baked in from the VM100 first deployment (2026-11-26):
#   1. Defender AMSI blocks unsigned input-injection scripts -- a SCOPED
#      ExclusionPath on the daemon directory is required BEFORE the first
#      run. Never use ExclusionProcess (machine-wide hole).
#   2. Serve payloads over the target's LAN, not tailnet -- VM guests may
#      have no tailscale client.
#   3. The daemon must run in the interactive user session: schedule an
#      interactive-token task. Direct execution from deployment channels
#      (qemu guest exec, WinRM) lands in session 0 where input/screenshots
#      are useless -- acceptable only as a compile smoke test.
#   4. ASCII-only script content: PowerShell 5.1 misreads UTF-8-no-BOM.
#
# Usage (as admin/SYSTEM, or any principal that can write ProgramData):
#   powershell -ep bypass -f install-winagentd.ps1 [-BaseUrl http://10.1.10.234:8077]

param(
    [string]$BaseUrl = "http://10.1.10.234:8077",
    [string]$InstallDir = "C:\ProgramData\winagentd",
    [string]$User = "voshudev",
    [int]$Port = 8177
)

$ErrorActionPreference = "Continue"
$log = Join-Path $InstallDir "install.log"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

function Log($msg) {
    "$((Get-Date).ToString('HH:mm:ss')) $msg" | Out-File $log -Append
}
"--- install winagentd $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ---" | Out-File $log -Force

# 1. Scoped Defender exclusion BEFORE the daemon file is ever executed.
#    Path-only: never exclude processes machine-wide.
try {
    Add-MpPreference -ExclusionPath $InstallDir -ErrorAction Stop
    Log "defender-exclusion: ok ($InstallDir)"
} catch {
    Log "defender-exclusion: FAILED - $($_.Exception.Message) (needs admin; daemon will be AMSI-blocked without it)"
}

# 2. Download the daemon payload.
try {
    Invoke-WebRequest -Uri "$BaseUrl/winagentd.ps1" -OutFile (Join-Path $InstallDir "winagentd.ps1") -UseBasicParsing -TimeoutSec 20 -ErrorAction Stop
    Log "download: ok from $BaseUrl"
} catch {
    Log "download: FAILED - $($_.Exception.Message)"
    if (!(Test-Path (Join-Path $InstallDir "winagentd.ps1"))) {
        Log "download: no existing payload, aborting"
        exit 1
    }
    Log "download: using previously staged payload"
}

# 3. Parse-verify the payload with this machine's own engine before installing.
$errs = $null; $tok = $null
[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $InstallDir "winagentd.ps1"), [ref]$tok, [ref]$errs) | Out-Null
if ($errs.Count -gt 0) {
    Log "parse-verify: FAILED ($($errs.Count) errors) - aborting, daemon not installed"
    exit 1
}
Log "parse-verify: ok"

# 4. Auth token (machine-local, never logged).
$tokenFile = Join-Path $InstallDir "token.txt"
if (!(Test-Path $tokenFile)) {
    (([guid]::NewGuid().Guid + [guid]::NewGuid().Guid) -replace "-", "") | Out-File -FilePath $tokenFile -NoNewline -Encoding ascii
    Log "token: generated"
} else {
    Log "token: exists"
}

# 5. Firewall: inbound on the daemon port (LAN+tailnet use; the token is the auth).
netsh advfirewall firewall delete rule name="WinAgentD" 2>&1 | Out-Null
netsh advfirewall firewall add rule name="WinAgentD" dir=in action=allow protocol=TCP localport=$Port 2>&1 | Out-Null
Log "firewall: inbound tcp/$Port allowed"

# 6. Interactive-token scheduled task: runs the daemon in the USER session
#    at logon; /it means no stored password, task only fires when the user
#    is logged in (which is exactly when a drivable desktop exists).
$tr = "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$InstallDir\winagentd.ps1`""
schtasks /create /tn WinAgentD /tr $tr /ru $User /it /sc onlogon /rl highest /f 2>&1 | Out-File $log -Append
Log "task: registered for $User (interactive)"

# 7. Restart the daemon now: kill any running instance, then fire the task.
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'winagentd' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 1
schtasks /run /tn WinAgentD 2>&1 | Out-File $log -Append
Start-Sleep 5

# 8. Verify: the listener must answer on the daemon port.
$listen = (Test-NetConnection 127.0.0.1 -Port $Port -WarningAction SilentlyContinue).TcpTestSucceeded
if ($listen) {
    $proc = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'winagentd' } | Select-Object -First 1
    Log "verify: LISTENING on $Port (session $($proc.SessionId), pid $($proc.ProcessId))"
    if ($proc -and $proc.SessionId -eq 0) {
        Log "verify: WARNING - running in session 0; input/screenshot will not drive the user desktop"
    }
    exit 0
} else {
    Log "verify: FAILED - port $Port not listening; check agent.log and AMSI"
    exit 1
}
