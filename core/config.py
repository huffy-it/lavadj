"""Toda la configuración del bot en un solo lugar.

Lo que se ajusta sin tocar código sale del `.env` (plantilla: `.env.example`), y
lo que es una decisión de diseño queda como constante con el porqué al lado.
"""

import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _entero(nombre: str, por_defecto: int) -> int:
    """Un entero del entorno, o el valor por defecto si viene cualquier cosa."""
    try:
        return int(os.getenv(nombre, por_defecto))
    except (TypeError, ValueError):
        return por_defecto


def _booleano(nombre: str, por_defecto: bool) -> bool:
    valor = os.getenv(nombre)
    if valor is None:
        return por_defecto
    return valor.strip().lower() in ("1", "true", "si", "sí", "on", "yes")


def _lista_de_texto(nombre: str, por_defecto: tuple[str, ...]) -> tuple[str, ...]:
    """Palabras separadas por comas del entorno, sin repetidas y en orden; vacía o ausente, el valor por defecto."""
    palabras = (p.strip() for p in os.getenv(nombre, "").split(","))
    return tuple(dict.fromkeys(p for p in palabras if p)) or por_defecto


def _lista_de_ids(nombre: str) -> tuple[frozenset[int], tuple[str, ...]]:
    """IDs numéricos del entorno, separados por comas y/o espacios.

    Devuelve (válidos, inválidos). Lo que no es un número se descarta, pero no en
    silencio: se entrega aparte para que quien arranca el bot lo avise en el log.
    Un ID mal escrito que desaparece sin dejar rastro deja a ese servidor fuera (o,
    si era el único, deja el bot sin ninguna restricción) y nadie se entera.
    """
    validos: set[int] = set()
    invalidos: list[str] = []
    for entrada in os.getenv(nombre, "").replace(",", " ").split():
        # `isdigit` solo no basta: acepta dígitos como "²" que `int` rechaza.
        if entrada.isascii() and entrada.isdigit():
            validos.add(int(entrada))
        else:
            invalidos.append(entrada)
    return frozenset(validos), tuple(invalidos)


# --- Discord -----------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
PREFIX = os.getenv("PREFIX", "!")

# Lista blanca de servidores. Vacía = sin restricción (lo de siempre): quien no
# tiene la variable en su `.env` no debe notar ningún cambio. Con entradas, el bot
# se va de los servidores nuevos que no estén y en los demás ignora los comandos.
SERVIDORES_PERMITIDOS, SERVIDORES_INVALIDOS = _lista_de_ids("SERVIDORES_PERMITIDOS")

# --- Lavalink ----------------------------------------------------------------
# El nodo de audio lo levanta scripts/lavalink/start-lavalink.sh (contenedor
# podman) o scripts/windows/lavalink.ps1 (directo con Java); el bot solo se conecta.
LAVALINK_URI = os.getenv("LAVALINK_URI", "http://127.0.0.1:2333")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")

# --- API HTTP de control (el panel de escritorio) ----------------------------
API_HOST = "127.0.0.1"          # nunca 0.0.0.0: no hay autenticación
API_PORT = _entero("API_PORT", 9876)

# --- Audio de YouTube --------------------------------------------------------
# El plugin de Lavalink ya no consigue sacar el audio de YouTube, así que lo
# resuelve yt-dlp y Lavalink solo reproduce la URL (ver core/resolver.py).
# En Linux yt-dlp suele vivir en ~/.local/bin, que no está en el PATH de una shell
# no interactiva: de ahí el fallback explícito.
def _buscar_ytdlp() -> str:
    """yt-dlp del PATH, del venv (donde lo deja pip en Windows) o de ~/.local/bin."""
    exe = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
    venv = Path(sys.executable).parent / exe
    return (
        shutil.which("yt-dlp")
        or (str(venv) if venv.is_file() else "")
        or os.path.expanduser("~/.local/bin/yt-dlp")
    )


YTDLP_BIN = os.getenv("YTDLP_BIN") or _buscar_ytdlp()

# El único cliente de YouTube que hoy entrega audio sin credenciales. Si se cae, el bot
# cambia solo de cliente, pero hasta reiniciar: lo fijo es lo que se ponga aquí.
YTDLP_PLAYER_CLIENT = os.getenv("YTDLP_PLAYER_CLIENT", "android")

# Los clientes que se prueban, en este orden, cuando el actual deja de dar audio (la lista de AGENTS.md).
YTDLP_CLIENTES = _lista_de_texto(
    "YTDLP_CLIENTES",
    ("android", "android_vr", "ios", "mweb", "web", "tv_simply", "web_embedded"),
)

# El bot actualiza yt-dlp cuando lo ve viejo o ningún cliente suena; YTDLP_AUTOACTUALIZAR=false lo apaga.
YTDLP_AUTOACTUALIZAR = _booleano("YTDLP_AUTOACTUALIZAR", True)

# Cuánto se le aguanta a yt-dlp por canción antes de darla por perdida.
YTDLP_TIMEOUT = _entero("YTDLP_TIMEOUT", 30)

# Al aire, con cola y sin que arranque ninguna canción durante este rato: el bot
# está mudo. Más que la canción más larga que se suele encolar, y menos que la
# paciencia de cualquiera.
SILENCIO_PARA_MUDO = _entero("SILENCIO_PARA_MUDO", 300)

# --- DJ ----------------------------------------------------------------------
# Volumen de la voz del DJ, en porcentaje de su nivel histórico (0-200). Es el
# valor con el que arranca el bot; el panel de escritorio lo mueve en vivo por
# la API y recuerda su propia última posición. 0 = DJ mudo.
DJ_VOLUME_DEFAULT = _entero("DJ_VOLUME", 100)

# --- Colas -------------------------------------------------------------------
# Tope duro de la cola. Sin esto, dos playlists seguidas dejan al bot con miles
# de temas encolados y el `!queue` inmanejable.
QUEUE_LIMIT = _entero("QUEUE_LIMIT", 500)

# Cuántos temas se toman de una playlist de una sola vez. Las listas "mix" y
# "radio" de YouTube son infinitas por diseño.
PLAYLIST_LIMIT = _entero("PLAYLIST_LIMIT", 100)

# --- Autoplay ----------------------------------------------------------------
# Cuando se vacía la cola, seguir solo con temas parecidos al último. Es la
# gracia de una radio, así que viene encendido; se apaga por servidor con
# `!autoplay off`.
AUTOPLAY_DEFAULT = _booleano("AUTOPLAY_DEFAULT", True)

# Sin esto el autoplay dejaría al bot tocando para nadie hasta el fin de los
# tiempos: si tras N canciones seguidas no quedó ningún humano en el canal, se
# corta la transmisión.
INACTIVE_CHANNEL_TOKENS = _entero("INACTIVE_CHANNEL_TOKENS", 3)

# Sin música y sin nadie pidiendo nada, el bot se va del canal.
IDLE_TIMEOUT = _entero("IDLE_TIMEOUT", 300)
