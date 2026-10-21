# Nodo Lavalink de LavaDJ en Windows, directo con Java (sin contenedor).
# Uso:  lavalink.ps1 [start|stop|status|run]
param([string]$Accion = "start")
$ErrorActionPreference = "Stop"
$Raiz = (Resolve-Path "$PSScriptRoot\..\..").Path
$Dir = Join-Path $Raiz "lavalink"
$Jar = Join-Path $Dir "Lavalink.jar"
$Pid_ = Join-Path $Dir "lavalink.pid"
$Puerto = if ($env:LAVALINK_PORT) { $env:LAVALINK_PORT } else { 2333 }
$Heap = if ($env:LAVALINK_HEAP) { $env:LAVALINK_HEAP } else { "768m" }
$Yml = Join-Path $Raiz "scripts\lavalink\application.yml"
$Clave = (Select-String -Path $Yml -Pattern '^\s*password:\s*"(.*)"' | Select-Object -First 1).Matches.Groups[1].Value

function Responde {
    try {
        Invoke-WebRequest "http://127.0.0.1:$Puerto/version" -Headers @{ Authorization = $Clave } -TimeoutSec 2 -UseBasicParsing | Out-Null
        $true
    } catch { $false }
}
function Proceso {
    # Tras un reinicio el PID del archivo puede ser de cualquier otro programa:
    # solo cuenta si ese proceso es java.
    if (Test-Path $Pid_) {
        $p = Get-Process -Id (Get-Content $Pid_) -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -eq 'java') { $p }
    }
}
function Argumentos {
    # Más heap que en Linux y G1: con 384m y GC serial las pausas atrasaban los
    # frames y el audio salía a tirones (acelerado y luego lento).
    # El application.yml escucha en 0.0.0.0 porque en Linux corre dentro de un
    # contenedor; aquí no hay contenedor y eso lo dejaría abierto a toda la red
    # con la contraseña por defecto. Solo local, como en Linux.
    @("-Xms64m", "-Xmx$Heap", "-XX:+UseG1GC", "-XX:MaxGCPauseMillis=50", "-XX:MaxMetaspaceSize=128m",
      "-Dserver.address=127.0.0.1", "-jar", "Lavalink.jar")
}
function Preparar {
    if (-not (Test-Path $Jar)) { throw "Falta $Jar. Corre scripts\windows\setup.ps1" }
    if (-not (Get-Command java -ErrorAction SilentlyContinue)) { throw "Falta Java. Corre scripts\windows\setup.ps1" }
    Copy-Item $Yml (Join-Path $Dir "application.yml") -Force
    New-Item -ItemType Directory -Force (Join-Path $Raiz "dj_cache") | Out-Null
}

switch ($Accion) {
    "start" {
        if (Responde) { Write-Host "Lavalink ya está arriba en el puerto $Puerto."; exit 0 }
        Preparar
        $p = Start-Process java -ArgumentList (Argumentos) -WorkingDirectory $Dir -WindowStyle Hidden -PassThru `
             -RedirectStandardOutput (Join-Path $Dir "stdout.log") -RedirectStandardError (Join-Path $Dir "stderr.log")
        $p.Id | Set-Content $Pid_
        Write-Host -NoNewline "Esperando a Lavalink"
        for ($i = 0; $i -lt 90; $i++) {
            if (Responde) { Write-Host " - listo en http://127.0.0.1:$Puerto"; exit 0 }
            if ($p.HasExited) { Write-Host " - se cayó:"; Get-Content (Join-Path $Dir "stdout.log") -Tail 30; exit 1 }
            Write-Host -NoNewline "."; Start-Sleep 1
        }
        Write-Host " - no respondió en 90 s"; exit 1
    }
    "stop" {
        $p = Proceso
        if ($p) { Stop-Process -Id $p.Id -Force; Remove-Item $Pid_ -ErrorAction SilentlyContinue; Write-Host "Lavalink detenido." }
        else {
            # Un pid viejo de otro proceso no se toca; solo se borra el archivo.
            Remove-Item $Pid_ -ErrorAction SilentlyContinue
            Write-Host "No estaba corriendo."
        }
    }
    "status" {
        if (Responde) { Write-Host "arriba (puerto $Puerto)" }
        elseif (Proceso) { Write-Host "arrancando"; exit 1 }
        else { Write-Host "abajo"; exit 1 }
    }
    "run" { Preparar; Set-Location $Dir; & java (Argumentos) }
    default { Write-Host "Uso: lavalink.ps1 [start|stop|status|run]"; exit 2 }
}
