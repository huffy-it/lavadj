"""Voz del DJ: Piper o espeak-ng → ffmpeg → wav.

Piper (voz neuronal, core/voces.py) corre como subproceso y manda cuando la voz
elegida es suya; si falla o no está, habla espeak y el DJ nunca se queda mudo.

espeak suena a robot, que es exactamente lo que se busca: el filtro de radio
(ver RADIO_FILTER) lo convierte en locutor de emisora pirata en vez de lector
de pantalla. Ese filtro se aplicaba al reproducir, cuando el audio salía por
ffmpeg desde el propio bot; con Lavalink el clip se manda como un track más y
nadie lo filtra, así que ahora se hornea en el wav apenas se sintetiza.

En la misma pasada entra el volumen del DJ: es un `volume=` más en la cadena,
que no toca el volumen del reproductor y por lo tanto no pelea con la música.

El texto SIEMPRE viaja como argumento de una lista a create_subprocess_exec,
nunca por una shell: los títulos de canciones vienen de YouTube y cualquiera
puede encolar un video llamado "; rm -rf ~".
"""

import asyncio
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

from core import ajustes, voces

log = logging.getLogger("tts")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "dj_cache"
CACHE_DIR.mkdir(exist_ok=True)

def _buscar_espeak() -> str | None:
    """espeak-ng del PATH o, en Windows, de donde lo deja su instalador."""
    encontrado = shutil.which("espeak-ng") or shutil.which("espeak")
    if encontrado:
        return encontrado
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidato = Path(base) / "eSpeak NG" / "espeak-ng.exe"
            if candidato.is_file():
                return str(candidato)
    return None


ESPEAK_BIN = _buscar_espeak()
FFMPEG_BIN = shutil.which("ffmpeg")

# Las frases del banco están en inglés y los titulares en castellano; leer una
# con la voz de la otra sale destrozado, así que el DJ elige según la categoría.
VOICE_ES = "es-419"   # español latinoamericano
VOICE_EN = "en-us"
VOICE = VOICE_ES

SPEED = "155"     # palabras por minuto
PITCH = "35"      # más grave que el default (50)

PIPER_LENGTH_SCALE = 1.0    # duración de cada fonema: más de 1 habla más lento

TIMEOUT = 10
PIPER_TIMEOUT = 30    # cada clip arranca onnxruntime y carga el modelo

# Banda telefónica + compresión: esto es lo que suena a radio.
RADIO_FILTER = "highpass=f=400,lowpass=f=3400,acompressor"

# La ganancia que llevaba el filtro de siempre. El volumen del DJ se expresa
# como un porcentaje DE ESTO, así que 100% suena exactamente como sonaba antes.
BASE_GAIN = 1.6

# Más de 200% empieza a saturar y el compresor no alcanza a tapar el destrozo.
MAX_VOLUME = 200


def filter_chain(volume: int, radio: bool = True) -> str:
    """La cadena de ffmpeg para un volumen dado, en porcentaje (0-200), con o sin filtro de radio."""
    pct = max(0, min(MAX_VOLUME, volume))
    if not radio:
        # El limitador evita que subir de 100% sature a Piper, que ya sale al tope.
        return f"volume={pct / 100:.2f},alimiter=limit=0.95:level=disabled"
    return f"{RADIO_FILTER},volume={BASE_GAIN * pct / 100:.2f}"


def available() -> bool:
    return ESPEAK_BIN is not None or (voces.piper_disponible() and bool(voces.instaladas()))


async def _correr(args: list[str], que: str, timeout: int = TIMEOUT, entrada: bytes | None = None) -> bool:
    """Corre un binario sin shell, con `entrada` por stdin si hay. True si terminó bien."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=None if entrada is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(entrada), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("%s no respondió en %ss", que, timeout)
        proc.kill()
        await proc.wait()      # sin esto queda zombi hasta que muera el bot
        return False
    except OSError:
        log.exception("no pude ejecutar %s", que)
        return False

    if proc.returncode != 0:
        log.warning("%s falló (%s): %s", que, proc.returncode,
                    stderr.decode(errors="replace")[:200])
        return False
    return True


def _tiene_audio(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _voz_piper(forzada: str | None) -> str | None:
    """La voz de Piper con la que hablar, o None si toca espeak."""
    voz = forzada or ajustes.obtener("voz")
    if voz == voces.ESPEAK and not ESPEAK_BIN:
        voz = next(iter(voces.instaladas()), voz)
    if voz == voces.ESPEAK:
        return None
    if voces.piper_disponible() and voces.instalada(voz):
        return voz
    log.warning("La voz %s no está disponible (¿falta piper-tts o la descarga?): uso espeak", voz)
    return None


async def _hablar(args: list[str], que: str, texto: str, crudo: Path, **kwargs) -> bool:
    """Corre el sintetizador y comprueba que dejó audio en `crudo`."""
    ok = await _correr(args, que, **kwargs)
    if ok and not _tiene_audio(crudo):
        log.warning("%s no escribió audio para %r", que, texto[:60])
        ok = False
    if not ok:
        cleanup(crudo)
    return ok


async def synthesize(text: str, voice: str = VOICE, volume: int = 100, voz: str | None = None) -> Path | None:
    """Genera el wav del clip. Devuelve None si falla (el DJ nunca es fatal).

    `voice` es el código de espeak; la voz de Piper sale de los ajustes, salvo que se fuerce con `voz`.
    """
    nombre = uuid.uuid4().hex
    crudo = CACHE_DIR / f"dj_{nombre}.crudo.wav"
    hablado = False
    # Las frases en inglés del banco las lee espeak: las voces de Piper del catálogo son en español.
    if piper := (_voz_piper(voz) if voz or voice != VOICE_EN or not ESPEAK_BIN else None):
        args = [
            sys.executable, "-X", "utf8", "-m", "piper",
            "-m", str(voces.ruta_modelo(piper)), "-c", str(voces.ruta_config(piper)),
            "-f", str(crudo), "--length-scale", str(PIPER_LENGTH_SCALE),
        ]
        # El texto va por stdin: como argumento, Piper lo leería como opciones.
        hablado = await _hablar(args, "piper", text, crudo, timeout=PIPER_TIMEOUT, entrada=text.encode("utf-8"))
        if not hablado:
            log.warning("Piper falló con la voz %s: sigo con espeak", piper)
    if not hablado and ESPEAK_BIN:
        args = [
            ESPEAK_BIN,
            "-v", voice,
            "-s", SPEED,
            "-p", PITCH,
            "-w", str(crudo),
            "--", text,          # -- corta el parseo de flags: el texto no es una opción
        ]
        hablado = await _hablar(args, "espeak", text, crudo)
    if not hablado:
        return None

    if not FFMPEG_BIN:
        # Sin ffmpeg se pierde el filtro de radio y el volumen, no la voz.
        log.warning("no hay ffmpeg: el DJ sale sin filtro de radio")
        return crudo

    filtrado = CACHE_DIR / f"dj_{nombre}.wav"
    filtro = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(crudo),
        "-af", filter_chain(volume, ajustes.obtener("filtro_radio")),
        str(filtrado),
    ]
    if not await _correr(filtro, "ffmpeg") or not _tiene_audio(filtrado):
        log.warning("no pude filtrar el clip; va la voz cruda")
        cleanup(filtrado)
        return crudo

    cleanup(crudo)
    return filtrado


def cleanup(path) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        log.warning("no pude borrar %s", path)


def purge_cache() -> int:
    """Vacía dj_cache/. Se llama al arrancar: si el bot murió a mitad de un
    clip, el wav queda huérfano y si no se limpia se acumulan para siempre."""
    removed = 0
    for leftover in CACHE_DIR.glob("dj_*.wav"):
        try:
            leftover.unlink()
            removed += 1
        except OSError:
            pass
    return removed
