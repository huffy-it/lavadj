# Crea el acceso directo "LavaDJ" en el escritorio.
$Raiz = (Resolve-Path "$PSScriptRoot\..\..").Path
$Escritorio = [Environment]::GetFolderPath("Desktop")
$Ws = New-Object -ComObject WScript.Shell
# Compat: quita el acceso directo de antes del cambio de nombre (Huffybot.lnk).
Remove-Item -ErrorAction SilentlyContinue "$Escritorio\Huffybot.lnk"
$Lnk = $Ws.CreateShortcut("$Escritorio\LavaDJ.lnk")
$Lnk.TargetPath = "powershell.exe"
$Lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Raiz\scripts\windows\start.ps1`""
$Lnk.WorkingDirectory = $Raiz
$Lnk.Description = "Levanta Lavalink y el bot LavaDJ"
$Lnk.Save()
Write-Host "Creado: $Escritorio\LavaDJ.lnk"
