# Prepara LavaDJ en Windows: Java, ffmpeg, espeak-ng, yt-dlp y Lavalink.jar.
# Uso (desde la raíz del proyecto):  powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
# Es repetible: lo que ya está instalado se salta.
$ErrorActionPreference = "Stop"
$Raiz = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $Raiz

function Tiene($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function InstalarPaquete($id) {
    Write-Host "-> winget install $id"
    winget install --id $id -e --accept-package-agreements --accept-source-agreements
}

# 1. venv + dependencias de Python (yt-dlp incluido: queda en .venv\Scripts)
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt yt-dlp

# 2. Programas del sistema
if (-not (Tiene java))       { InstalarPaquete "EclipseAdoptium.Temurin.21.JRE" }
if (-not (Tiene ffmpeg))     { InstalarPaquete "Gyan.FFmpeg" }
$espeak = (Tiene espeak-ng) -or (Test-Path "$env:ProgramFiles\eSpeak NG\espeak-ng.exe")
if (-not $espeak)            { InstalarPaquete "eSpeak-NG.eSpeak-NG" }

# 3. Lavalink.jar
New-Item -ItemType Directory -Force lavalink | Out-Null
if (-not (Test-Path lavalink\Lavalink.jar)) {
    Write-Host "-> bajando Lavalink.jar (github.com/lavalink-devs/Lavalink, ~80 MB)"
    Invoke-WebRequest "https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar" -OutFile lavalink\Lavalink.jar
}

# 4. Archivos de configuración
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "-> creé .env: pon adentro tu DISCORD_TOKEN" }
if (-not (Test-Path dj_feeds.json)) { Copy-Item dj_feeds.example.json dj_feeds.json }

Write-Host "`nListo. Si winget instaló algo, abre una terminal NUEVA para que tome el PATH."
Write-Host "Después: scripts\windows\start.ps1"
