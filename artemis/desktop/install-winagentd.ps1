$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force -Path C:\ProgramData\winagentd | Out-Null
Invoke-WebRequest -Uri "http://100.127.151.100:8077/winagentd.ps1" -OutFile "C:\ProgramData\winagentd\winagentd.ps1" -UseBasicParsing
if (!(Test-Path C:\ProgramData\winagentd\token.txt)) {
    (([guid]::NewGuid().Guid + [guid]::NewGuid().Guid) -replace "-","") | Out-File -FilePath C:\ProgramData\winagentd\token.txt -NoNewline -Encoding ascii
}
netsh advfirewall firewall add rule name="WinAgentD" dir=in action=allow protocol=TCP localport=8177 2>&1 | Out-Null
schtasks /create /tn WinAgentD /tr "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\ProgramData\winagentd\winagentd.ps1" /ru voshudev /it /sc onlogon /rl highest /f 2>&1
schtasks /end /tn WinAgentD 2>&1 | Out-Null
Start-Sleep -Seconds 1
schtasks /run /tn WinAgentD 2>&1
Start-Sleep -Seconds 3
$tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port 8177 -WarningAction SilentlyContinue
"LISTENING=$($tcp.TcpTestSucceeded)"
