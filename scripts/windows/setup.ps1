# Prepara LavaDJ en Windows: Java, ffmpeg, espeak-ng, yt-dlp, Lavalink.jar y su plugin de YouTube.
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

# 1. venv + dependencias de Python (yt-dlp viene en requirements.txt y queda en .venv\Scripts)
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

# 2. Programas del sistema
if (-not (Tiene java))       { InstalarPaquete "EclipseAdoptium.Temurin.21.JRE" }
if (-not (Tiene ffmpeg))     { InstalarPaquete "Gyan.FFmpeg" }
$espeak = (Tiene espeak-ng) -or (Test-Path "$env:ProgramFiles\eSpeak NG\espeak-ng.exe")
if (-not $espeak)            { InstalarPaquete "eSpeak-NG.eSpeak-NG" }

# 3. Lavalink.jar y el plugin de YouTube
New-Item -ItemType Directory -Force lavalink | Out-Null
if (-not (Test-Path lavalink\Lavalink.jar)) {
    Write-Host "-> bajando Lavalink.jar (github.com/lavalink-devs/Lavalink, ~80 MB)"
    Invoke-WebRequest "https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar" -OutFile lavalink\Lavalink.jar
}

# El plugin se instala a mano y no con `dependency` en el application.yml: el
# repositorio maven.lavalink.dev devuelve HTML en vez del XML que Lavalink espera
# y Lavalink no arranca. Para cambiar de versión, cambia $PluginVersion y
# $PluginSha256 (la URL se arma sola; el hash sale de bajar el jar a mano y correr
# Get-FileHash), los mismos datos en start-lavalink.sh y el nombre del jar en
# lavalink.ps1; y borra el jar viejo de lavalink\plugins para que Lavalink no
# cargue los dos.
$PluginVersion = "1.18.2"
$PluginArchivo = "youtube-plugin-$PluginVersion.jar"
$PluginUrl     = "https://github.com/lavalink-devs/youtube-source/releases/download/$PluginVersion/$PluginArchivo"
$PluginSha256  = "dd4b3bce50dbc7582c80776bad47bad87bef1dc683c69d66ab4a084aa3006238"
# Se mira solo si el archivo existe, no su hash: el que bajó Lavalink antes desde
# Maven es otro build de la misma versión y no hay por qué reemplazarlo.
if (-not (Test-Path "lavalink\plugins\$PluginArchivo")) {
    New-Item -ItemType Directory -Force lavalink\plugins | Out-Null
    Write-Host "-> bajando $PluginArchivo (github.com/lavalink-devs/youtube-source, ~1.6 MB)"
    # Se baja a un archivo aparte y solo recibe su nombre definitivo si el hash
    # cuadra: una descarga cortada o corrupta nunca queda como jar bueno.
    $Temporal = "lavalink\plugins\$PluginArchivo.descarga"
    try {
        Invoke-WebRequest $PluginUrl -OutFile $Temporal
        $Hash = (Get-FileHash $Temporal -Algorithm SHA256).Hash
        if ($Hash -ne $PluginSha256) {
            throw "El SHA-256 de $PluginArchivo no coincide: esperaba $PluginSha256 y llegó $Hash. Lo borré; vuelve a correr setup.ps1 y, si se repite, revisa $PluginUrl."
        }
        Move-Item $Temporal "lavalink\plugins\$PluginArchivo"
    } finally {
        Remove-Item $Temporal -ErrorAction SilentlyContinue
    }
}

# 4. Archivos de configuración
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "-> creé .env: pon adentro tu DISCORD_TOKEN" }
if (-not (Test-Path dj_feeds.json)) { Copy-Item dj_feeds.example.json dj_feeds.json }

Write-Host "`nListo. Si winget instaló algo, abre una terminal NUEVA para que tome el PATH."
Write-Host "Después: scripts\windows\start.ps1"
