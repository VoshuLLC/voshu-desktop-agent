# winagentd.ps1 — Windows desktop agent daemon for ARTEMIS (VOSHU-DEVICE-CONTROL).
#
# Mirror of macagentd.py: the same HTTP contract so artemis/drivers/desktop/
# http_driver.py can drive a Windows machine exactly like a macOS one:
#
#   GET  /health       liveness + screen size + capability report          (no auth)
#   GET  /screenshot   PNG (or ?format=jpg) — GDI CopyFromScreen
#   GET  /frame        alias of /screenshot (fleet tile path)
#   GET  /hierarchy    UIAutomation element tree of the visible desktop
#   POST /input        {action, x, y, duration_ms?, times?, dx?, dy?}
#                      tap/double/right/long/move/swipe/scroll via SendInput
#   POST /type         {text, clear_existing?} — KEYEVENTF_UNICODE keystrokes
#   POST /key          {key} — virtual-key map (ENTER/TAB/ESC/BACK/HOME/arrows...)
#   POST /shell        {cmd, timeout_s?} — powershell child process
#   POST /app          {action: launch|terminate, name}
#
# Every route except /health requires `Authorization: Bearer <token>`.
# The token lives in C:\ProgramData\winagentd\token.txt (machine-local only).
#
# Zero dependencies: PowerShell 5.1 + .NET Framework, both ship with Windows.
# Input goes through SendInput — the kernel input path — so it is
# indistinguishable from a human at the machine, and the real system cursor
# moves: anyone watching the console sees the ghost cursor work.
#
# MUST run inside the interactive user session (a service in session 0
# cannot see or drive the user desktop). Deploy as a scheduled task with
# interactive token — see docs or the voshu-rc deploy scripts.

param(
    [int]$Port = 8177,
    [string]$TokenFile = "C:\ProgramData\winagentd\token.txt"
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- win32 input

$win32 = @"
using System;
using System.Runtime.InteropServices;

public static class W32 {
    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT {
        public int dx; public int dy; public uint mouseData;
        public uint dwFlags; public uint time; public UIntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT {
        public ushort wVk; public ushort wScan; public uint dwFlags;
        public uint time; public UIntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Explicit)]
    public struct INPUTUNION {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT {
        public uint type;
        public INPUTUNION u;
    }

    public const uint INPUT_MOUSE = 0;
    public const uint INPUT_KEYBOARD = 1;
    public const uint MOUSEEVENTF_MOVE = 0x0001;
    public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    public const uint MOUSEEVENTF_LEFTUP = 0x0004;
    public const uint MOUSEEVENTF_RIGHTDOWN = 0x0008;
    public const uint MOUSEEVENTF_RIGHTUP = 0x0010;
    public const uint MOUSEEVENTF_MIDDLEDOWN = 0x0020;
    public const uint MOUSEEVENTF_MIDDLEUP = 0x0040;
    public const uint MOUSEEVENTF_WHEEL = 0x0800;
    public const uint MOUSEEVENTF_ABSOLUTE = 0x8000;
    public const uint KEYEVENTF_KEYUP = 0x0002;
    public const uint KEYEVENTF_UNICODE = 0x0004;

    [DllImport("user32.dll", SetLastError = true)]
    public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")]
    public static extern int GetSystemMetrics(int nIndex);
    [DllImport("user32.dll")]
    public static extern short VkKeyScanW(char ch);

    public static int ScreenW { get { return GetSystemMetrics(0); } }
    public static int ScreenH { get { return GetSystemMetrics(1); } }

    static INPUT MouseInput(uint flags, int x, int y, uint data) {
        INPUT i = new INPUT();
        i.type = INPUT_MOUSE;
        i.u.mi.dx = x; i.u.mi.dy = y;
        i.u.mi.mouseData = data;
        i.u.mi.dwFlags = flags;
        i.u.mi.time = 0;
        i.u.mi.dwExtraInfo = UIntPtr.Zero;
        return i;
    }
    static INPUT KeyInput(ushort vk, ushort scan, uint flags) {
        INPUT i = new INPUT();
        i.type = INPUT_KEYBOARD;
        i.u.ki.wVk = vk; i.u.ki.wScan = scan;
        i.u.ki.dwFlags = flags;
        i.u.ki.time = 0;
        i.u.ki.dwExtraInfo = UIntPtr.Zero;
        return i;
    }

    static void AbsXY(int x, int y, out int ax, out int ay) {
        ax = (int)((x * 65535.0) / Math.Max(1, ScreenW - 1));
        ay = (int)((y * 65535.0) / Math.Max(1, ScreenH - 1));
    }

    public static void MoveTo(int x, int y) {
        SetCursorPos(x, y);
    }
    public static void ClickAt(int x, int y, string button, int times, int holdMs) {
        MoveTo(x, y);
        System.Threading.Thread.Sleep(30);
        uint down, up;
        if (button == "right") { down = MOUSEEVENTF_RIGHTDOWN; up = MOUSEEVENTF_RIGHTUP; }
        else if (button == "middle") { down = MOUSEEVENTF_MIDDLEDOWN; up = MOUSEEVENTF_MIDDLEUP; }
        else { down = MOUSEEVENTF_LEFTDOWN; up = MOUSEEVENTF_LEFTUP; }
        for (int n = 0; n < Math.Max(1, times); n++) {
            int ax, ay; AbsXY(x, y, out ax, out ay);
            INPUT[] seq = new INPUT[] {
                MouseInput(down | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, ax, ay, 0)
            };
            SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
            System.Threading.Thread.Sleep(Math.Max(20, holdMs));
            seq[0] = MouseInput(up | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, ax, ay, 0);
            SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
            if (n < times - 1) System.Threading.Thread.Sleep(80);
        }
    }
    public static void HoldAt(int x, int y, int durationMs) {
        MoveTo(x, y);
        System.Threading.Thread.Sleep(30);
        int ax, ay; AbsXY(x, y, out ax, out ay);
        INPUT[] seq = new INPUT[] { MouseInput(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, ax, ay, 0) };
        SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
        System.Threading.Thread.Sleep(Math.Max(100, durationMs));
        seq[0] = MouseInput(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, ax, ay, 0);
        SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
    }
    public static void DragPath(int x0, int y0, int x1, int y1, int durationMs) {
        MoveTo(x0, y0);
        System.Threading.Thread.Sleep(30);
        int ax, ay; AbsXY(x0, y0, out ax, out ay);
        INPUT[] seq = new INPUT[] { MouseInput(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, ax, ay, 0) };
        SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
        int steps = Math.Max(4, durationMs / 16);
        for (int s = 1; s <= steps; s++) {
            int cx = x0 + (x1 - x0) * s / steps;
            int cy = y0 + (y1 - y0) * s / steps;
            SetCursorPos(cx, cy);
            System.Threading.Thread.Sleep(Math.Max(8, durationMs / steps));
        }
        AbsXY(x1, y1, out ax, out ay);
        seq[0] = MouseInput(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, ax, ay, 0);
        SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
    }
    public static void Scroll(int x, int y, int dy) {
        MoveTo(x, y);
        INPUT[] seq = new INPUT[] { MouseInput(MOUSEEVENTF_WHEEL, 0, 0, (uint)dy) };
        SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
    }
    public static void TypeUnicode(string text) {
        foreach (char ch in text) {
            ushort u = (ushort)ch;
            INPUT[] seq = new INPUT[] { KeyInput(0, u, KEYEVENTF_UNICODE) };
            SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
            seq[0] = KeyInput(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP);
            SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
            System.Threading.Thread.Sleep(8);
        }
    }
    public static void TapKey(ushort vk) {
        INPUT[] seq = new INPUT[] { KeyInput(vk, 0, 0) };
        SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
        System.Threading.Thread.Sleep(30);
        seq[0] = KeyInput(vk, 0, KEYEVENTF_KEYUP);
        SendInput(1, seq, Marshal.SizeOf(typeof(INPUT)));
    }
}
"@

Add-Type -TypeDefinition $win32

Add-Type -AssemblyName System.Drawing

$script:uiaOk = $false
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $script:uiaOk = $true
} catch { }

# --------------------------------------------------------------------- state

$script:Token = ""
if (Test-Path $TokenFile) {
    $script:Token = (Get-Content $TokenFile -Raw).Trim()
}

function Write-Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    try { Add-Content -Path "C:\ProgramData\winagentd\agent.log" -Value $line -ErrorAction SilentlyContinue } catch { }
}

# -------------------------------------------------------------------- helpers

function Send-JsonResponse($stream, [int]$status, $obj) {
    $json = ($obj | ConvertTo-Json -Depth 8 -Compress)
    $body = [System.Text.Encoding]::UTF8.GetBytes($json)
    $reason = if ($status -eq 200) { "OK" } elseif ($status -eq 401) { "Unauthorized" } elseif ($status -eq 404) { "Not Found" } elseif ($status -eq 501) { "Not Implemented" } else { "Error" }
    $head = "HTTP/1.1 $status $reason`r`nContent-Type: application/json`r`nContent-Length: $($body.Length)`r`nAccess-Control-Allow-Origin: *`r`nConnection: close`r`n`r`n"
    $hb = [System.Text.Encoding]::ASCII.GetBytes($head)
    $stream.Write($hb, 0, $hb.Length)
    $stream.Write($body, 0, $body.Length)
}

function Send-BytesResponse($stream, [byte[]]$body, [string]$contentType) {
    $head = "HTTP/1.1 200 OK`r`nContent-Type: $contentType`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n"
    $hb = [System.Text.Encoding]::ASCII.GetBytes($head)
    $stream.Write($hb, 0, $hb.Length)
    $stream.Write($body, 0, $body.Length)
}

function Read-Request($stream) {
    # Minimal HTTP/1.x parser: headers then body by Content-Length. Requests
    # here are small (JSON bodies); read into memory with a generous cap.
    $buf = New-Object byte[] 65536
    $ms = New-Object System.IO.MemoryStream
    $total = 0
    $headerEnd = -1
    while ($headerEnd -lt 0 -and $total -lt 262144) {
        $n = $stream.Read($buf, 0, $buf.Length)
        if ($n -le 0) { return $null }
        $ms.Write($buf, 0, $n)
        $total += $n
        $all = $ms.ToArray()
        for ($i = 0; $i -lt $all.Length - 3; $i++) {
            if ($all[$i] -eq 13 -and $all[$i+1] -eq 10 -and $all[$i+2] -eq 13 -and $all[$i+3] -eq 10) {
                $headerEnd = $i + 4; break
            }
        }
    }
    if ($headerEnd -lt 0) { return $null }
    $all = $ms.ToArray()
    $headText = [System.Text.Encoding]::ASCII.GetString($all, 0, $headerEnd)
    $lines = $headText -split "`r`n"
    $parts = $lines[0] -split " "
    $method = $parts[0].ToUpper()
    $path = $parts[1]
    $cl = 0
    foreach ($ln in $lines) {
        if ($ln -match "(?i)^Content-Length:\s*(\d+)") { $cl = [int]$Matches[1] }
    }
    $have = $all.Length - $headerEnd
    while ($have -lt $cl) {
        $n = $stream.Read($buf, 0, $buf.Length)
        if ($n -le 0) { break }
        $ms.Write($buf, 0, $n)
        $have += $n
    }
    $all = $ms.ToArray()
    $bodyText = ""
    if ($cl -gt 0) {
        $bodyText = [System.Text.Encoding]::UTF8.GetString($all, $headerEnd, [Math]::Min($cl, $all.Length - $headerEnd))
    }
    return @{
        Method = $method
        Path   = $path
        Body   = $bodyText
        Head   = $headText
    }
}

# ------------------------------------------------------------------ handlers

function Get-ScreenshotBytes([string]$fmt) {
    $w = [W32]::ScreenW; $h = [W32]::ScreenH
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen(0, 0, 0, 0, (New-Object System.Drawing.Size $w, $h))
    $ms = New-Object System.IO.MemoryStream
    if ($fmt -eq "jpg" -or $fmt -eq "jpeg") {
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg)
    } else {
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    $g.Dispose(); $bmp.Dispose()
    return $ms.ToArray()
}

function Get-UiaHierarchy() {
    if (-not $script:uiaOk) {
        return @{ ok = $false; error = "UIAutomation assemblies unavailable"; elements = @() }
    }
    $elements = New-Object System.Collections.ArrayList
    $budget = 500
    $maxDepth = 14
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker

    function Walk($el, [int]$depth) {
        if ($null -eq $el -or $depth -gt $maxDepth) { return }
        if ($elements.Count -ge $budget) { return }
        try {
            $cur = $el.Current
            $r = $cur.BoundingRectangle
            $ct = $cur.ControlType.ProgrammaticName -replace "^ControlType\.", ""
            $name = [string]$cur.Name
            $include = $false
            if ($r.Width -gt 0 -and $r.Height -gt 0 -and -not $cur.IsOffscreen) {
                if ($name -and $name.Trim().Length -gt 0) { $include = $true }
                if ($ct -in @("Button","Edit","Hyperlink","MenuItem","TabItem","ListItem","CheckBox","RadioButton","ComboBox","Text","Document","Spinner","Slider","TreeItem","DataItem")) { $include = $true }
            }
            if ($include) {
                [void]$elements.Add(@{
                    id       = $elements.Count
                    name     = $name
                    role     = $ct
                    x        = [int][Math]::Round($r.X)
                    y        = [int][Math]::Round($r.Y)
                    w        = [int][Math]::Round($r.Width)
                    h        = [int][Math]::Round($r.Height)
                    cx       = [int][Math]::Round($r.X + $r.Width / 2)
                    cy       = [int][Math]::Round($r.Y + $r.Height / 2)
                    enabled  = [bool]$cur.IsEnabled
                    depth    = $depth
                })
            }
        } catch { }
        try {
            $child = $walker.GetFirstChild($el)
            while ($null -ne $child -and $elements.Count -lt $budget) {
                Walk $child ($depth + 1)
                $child = $walker.GetNextSibling($child)
            }
        } catch { }
    }

    try {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        Walk $root 0
    } catch {
        return @{ ok = $false; error = "uia walk failed: $($_.Exception.Message)"; elements = @() }
    }
    return @{ ok = $true; count = $elements.Count; elements = @($elements) }
}

function Invoke-ShellCmd([string]$cmd, [int]$timeoutS) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell.exe"
    $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -Command " + $cmd
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $done = $p.WaitForExit([Math]::Max(2000, $timeoutS * 1000))
    if (-not $done) {
        try { $p.Kill() } catch { }
        return @{ ok = $false; error = "timeout"; out = ""; err = ""; rc = -1 }
    }
    return @{
        ok  = $true
        out = $p.StandardOutput.ReadToEnd()
        err = $p.StandardError.ReadToEnd()
        rc  = $p.ExitCode
    }
}

$VK = @{
    "ENTER" = 0x0D; "TAB" = 0x09; "ESC" = 0x1B; "ESCAPE" = 0x1B
    "BACK" = 0x08; "BACKSPACE" = 0x08; "DELETE" = 0x2E; "HOME" = 0x24
    "END" = 0x23; "UP" = 0x26; "DOWN" = 0x28; "LEFT" = 0x25; "RIGHT" = 0x27
    "SPACE" = 0x20; "WIN" = 0x5B; "VOLUME_UP" = 0xAF; "VOLUME_DOWN" = 0xAE
    "PAGEUP" = 0x21; "PAGEDOWN" = 0x22
}

# --------------------------------------------------------------- main loop

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $Port)
$listener.Start()
Write-Log "winagentd listening on :$Port"

while ($true) {
    $client = $listener.AcceptTcpClient()
    try {
        $stream = $client.GetStream()
        $stream.ReadTimeout = 15000
        $req = Read-Request $stream
        if ($null -eq $req) { $client.Close(); continue }

        $pathOnly = ($req.Path -split "\?")[0]
        $query = ""
        if ($req.Path -match "\?(.*)$") { $query = $Matches[1] }

        # auth — /health open, everything else needs the bearer token
        if ($pathOnly -ne "/health") {
            $authed = $false
            if ($script:Token.Length -gt 0 -and $req.Head -match "(?i)Authorization:\s*Bearer\s+(\S+)") {
                if ($Matches[1] -eq $script:Token) { $authed = $true }
            }
            if (-not $authed) {
                Send-JsonResponse $stream 401 @{ ok = $false; error = "unauthorized" }
                $client.Close(); continue
            }
        }

        $body = @{}
        if ($req.Body -and $req.Body.Trim().Length -gt 0) {
            try { $body = $req.Body | ConvertFrom-Json } catch { $body = @{} }
        }

        switch -Regex ($pathOnly) {
            "^/health$" {
                Send-JsonResponse $stream 200 @{
                    ok = $true
                    screen = @{ w = [W32]::ScreenW; h = [W32]::ScreenH; unit = "points" }
                    caps = @{ screenshot = $true; hierarchy = $script:uiaOk; input = $true; shell = $true; stream = $false }
                    platform = "win"
                    version = "0.1.0"
                }
            }
            "^/(screenshot|frame)$" {
                $fmt = "png"
                if ($query -match "format=(\w+)") { $fmt = $Matches[1].ToLower() }
                $bytes = Get-ScreenshotBytes $fmt
                $ct = if ($fmt -match "jpe?g") { "image/jpeg" } else { "image/png" }
                Send-BytesResponse $stream $bytes $ct
            }
            "^/hierarchy$" {
                $t0 = Get-Date
                $h = Get-UiaHierarchy
                $h["elapsed_ms"] = [int]((Get-Date) - $t0).TotalMilliseconds
                Send-JsonResponse $stream 200 $h
            }
            "^/input$" {
                $action = [string]$body.action
                $x = [int]$body.x; $y = [int]$body.y
                switch ($action) {
                    "tap"    { [W32]::ClickAt($x, $y, "left", 1, 50); Send-JsonResponse $stream 200 @{ ok = $true } }
                    "double" { [W32]::ClickAt($x, $y, "left", 2, 50); Send-JsonResponse $stream 200 @{ ok = $true } }
                    "right"  { [W32]::ClickAt($x, $y, "right", 1, 50); Send-JsonResponse $stream 200 @{ ok = $true } }
                    "long"   {
                        $dur = 1000; if ($body.duration_ms) { $dur = [int]$body.duration_ms }
                        [W32]::HoldAt($x, $y, $dur); Send-JsonResponse $stream 200 @{ ok = $true }
                    }
                    "move"   { [W32]::MoveTo($x, $y); Send-JsonResponse $stream 200 @{ ok = $true } }
                    "swipe"  {
                        $ex = $x; $ey = $y
                        if ($null -ne $body.end_x) { $ex = [int]$body.end_x } elseif ($null -ne $body.dx) { $ex = $x + [int]$body.dx }
                        if ($null -ne $body.end_y) { $ey = [int]$body.end_y } elseif ($null -ne $body.dy) { $ey = $y + [int]$body.dy }
                        $dur = 400; if ($body.duration_ms) { $dur = [int]$body.duration_ms }
                        [W32]::DragPath($x, $y, $ex, $ey, $dur)
                        Send-JsonResponse $stream 200 @{ ok = $true }
                    }
                    "scroll" {
                        $dyv = -480; if ($null -ne $body.dy) { $dyv = [int]$body.dy }
                        [W32]::Scroll($x, $y, $dyv); Send-JsonResponse $stream 200 @{ ok = $true }
                    }
                    default  { Send-JsonResponse $stream 400 @{ ok = $false; error = "unknown action: $action" } }
                }
            }
            "^/type$" {
                $text = [string]$body.text
                if ($text.Length -eq 0) { Send-JsonResponse $stream 400 @{ ok = $false; error = "empty text" } }
                else {
                    # NOTE: clear_existing is accepted but not yet implemented
                    # (needs a ctrl+a chord helper); v1 types at cursor.
                    [W32]::TypeUnicode($text)
                    Send-JsonResponse $stream 200 @{ ok = $true; len = $text.Length }
                }
            }
            "^/key$" {
                $k = ([string]$body.key).ToUpper()
                if ($k -eq "POWER" -or $k -eq "APP_SWITCH") {
                    Send-JsonResponse $stream 501 @{ ok = $false; error = "$k unsupported on desktop" }
                } elseif ($VK.ContainsKey($k)) {
                    [W32]::TapKey([uint16]$VK[$k])
                    Send-JsonResponse $stream 200 @{ ok = $true }
                } else {
                    Send-JsonResponse $stream 400 @{ ok = $false; error = "unknown key: $k"; known = @($VK.Keys) }
                }
            }
            "^/shell$" {
                $cmd = [string]$body.cmd
                $to = 15
                if ($body.timeout_s) { $to = [int]$body.timeout_s }
                if ($cmd.Length -eq 0) {
                    Send-JsonResponse $stream 400 @{ ok = $false; error = "empty cmd" }
                } else {
                    $res = Invoke-ShellCmd $cmd $to
                    Send-JsonResponse $stream 200 $res
                }
            }
            "^/app$" {
                $action = [string]$body.action
                $name = [string]$body.name
                try {
                    if ($action -eq "launch") {
                        Start-Process $name
                        Send-JsonResponse $stream 200 @{ ok = $true }
                    } elseif ($action -eq "terminate") {
                        $proc = Get-Process -Name $name -ErrorAction SilentlyContinue
                        if ($proc) { $proc | Stop-Process -Force; Send-JsonResponse $stream 200 @{ ok = $true } }
                        else { Send-JsonResponse $stream 404 @{ ok = $false; error = "not running: $name" } }
                    } else {
                        Send-JsonResponse $stream 400 @{ ok = $false; error = "unknown app action: $action" }
                    }
                } catch {
                    Send-JsonResponse $stream 500 @{ ok = $false; error = $_.Exception.Message }
                }
            }
            default {
                Send-JsonResponse $stream 404 @{ ok = $false; error = "no route: $pathOnly" }
            }
        }
    } catch {
        Write-Log "request error: $($_.Exception.Message)"
        try { Send-JsonResponse $stream 500 @{ ok = $false; error = $_.Exception.Message } } catch { }
    } finally {
        try { $client.Close() } catch { }
    }
}
