# Levanta Lavalink (si hace falta), abre el panel web y corre el bot en esta
# ventana (se ve el log). Ctrl+C o cerrar la ventana apaga el bot; Lavalink
# queda abajo con:  scripts\windows\lavalink.ps1 stop
$ErrorActionPreference = "Stop"
$Raiz = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $Raiz
& "$PSScriptRoot\lavalink.ps1" start
if ($LASTEXITCODE -ne 0) { Read-Host "Lavalink no levantó. Enter para cerrar"; exit 1 }
Start-Job { Start-Sleep 8; Start-Process "http://127.0.0.1:9876/" } | Out-Null
& .\.venv\Scripts\python.exe bot.py
