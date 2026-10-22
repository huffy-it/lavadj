"""Búsqueda de letras (lrclib con fallback a lyrics.ovh), con tiempos cuando los hay."""

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import NamedTuple
from urllib.parse import quote

import aiohttp

log = logging.getLogger("lyrics")

MAX_CHARS = 1800
REQUEST_TIMEOUT = 8
TOTAL_TIMEOUT = 25
CACHE_MAXIMA = 200
CACHE_VACIA_S = 600

TITLE_NOISE_RE = re.compile(
    r"\s*(\(|\[).*(official|video|audio|lyrics?|lyric video|HD|4K|visualizer|sub español|sub esp|remaster|live|en vivo).*(\)|\])",
    re.IGNORECASE,
)
TRAILING_TITLE_NOISE_RE = re.compile(
    r"\s+[-|]?\s*(official\s+)?(music\s+)?(video|audio|lyrics?|lyric video|visualizer|remaster(ed)?|live|en vivo)\s*$",
    re.IGNORECASE,
)
LIVE_SESSION_RE = re.compile(r"\s+(desde|en vivo desde|live at|live from)\b.*$", re.IGNORECASE)
FEAT_RE = re.compile(r"\s*(\(|\[)?\s*(feat\.?|ft\.?|featuring)\s+[^)\]]+(\)|\])?", re.IGNORECASE)
LRC_TIMESTAMP_RE = re.compile(r"^\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]\s*", re.MULTILINE)
LRC_LINE_RE = re.compile(r"^((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)(.*)$")
LRC_MARK_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
LRC_OFFSET_RE = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]\s*$", re.IGNORECASE)
AUTHOR_NOISE_RE = re.compile(r"\s*(-\s*Topic|VEVO)$", re.IGNORECASE)


class Letra(NamedTuple):
    """Una letra lista para mostrar. Sin sincronizar, el tiempo de cada línea es None."""

    sincronizada: bool
    lineas: list[tuple[int | None, str]]


def clean_song_title(title: str) -> str:
    title = TITLE_NOISE_RE.sub("", title)
    title = LIVE_SESSION_RE.sub("", title)
    title = TRAILING_TITLE_NOISE_RE.sub("", title)
    title = FEAT_RE.sub("", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip(" -|")


def split_artist_song(title: str) -> tuple[str, str]:
    title = clean_song_title(title)
    for separator in (" - ", " – ", " — ", " | "):
        if separator in title:
            artist, song = title.split(separator, 1)
            return artist.strip(), song.strip()
    return "", title


def clean_lyrics_text(text: str) -> str:
    text = LRC_TIMESTAMP_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def search_terms(title: str, artist: str, song: str) -> list[str]:
    clean_title = clean_song_title(title)
    clean_song = clean_song_title(song)
    terms = []
    seen = set()
    for term in (
        f"{artist} {clean_song}" if artist else "",
        f"{clean_song} {artist}" if artist else "",
        clean_title,
        clean_song,
        re.sub(r"^(el|la|los|las)\s+", "", clean_song, flags=re.IGNORECASE),
    ):
        term = term.strip()
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def artist_and_song(title: str, author: str = "") -> tuple[str, str]:
    """Artista y canción de una pista: del título si trae "Artista - Canción"; si no, del canal."""
    artist, song = split_artist_song(title)
    return artist or AUTHOR_NOISE_RE.sub("", author or "").strip(), song


def parse_lrc(text: str) -> list[tuple[int, str]]:
    """Las líneas con tiempo de una letra LRC como (ms, texto), ordenadas.

    Una línea con varias marcas (`[00:12.34][01:02.00]texto`) cuenta una vez por marca.
    Las etiquetas (`[ar:...]`, `[ti:...]`) se descartan, salvo `[offset:ms]`, que
    adelanta (+) o atrasa (-) todos los tiempos. Una marca sin texto es una pausa
    instrumental: se conserva, menos al principio, al final y repetida.
    """
    marcas, offset = [], 0
    for linea in (text or "").splitlines():
        linea = linea.strip()
        if match := LRC_OFFSET_RE.match(linea):
            offset = int(match.group(1))
        elif match := LRC_LINE_RE.match(linea):
            texto = match.group(2).strip()
            for minutos, segundos, fraccion in LRC_MARK_RE.findall(match.group(1)):
                ms = (int(minutos) * 60 + int(segundos)) * 1000 + int((fraccion or "0").ljust(3, "0"))
                marcas.append((ms, texto))
    lineas: list[tuple[int, str]] = []
    for ms, texto in sorted(((max(0, ms - offset), t) for ms, t in marcas), key=lambda marca: marca[0]):
        if texto or (lineas and lineas[-1][1]):
            lineas.append((ms, texto))
    while lineas and not lineas[-1][1]:
        lineas.pop()
    return lineas


def plain_letra(text: str) -> Letra | None:
    """Una letra sin tiempos, línea por línea; None si no queda nada."""
    text = clean_lyrics_text(text or "")
    return Letra(False, [(None, linea) for linea in text.splitlines()]) if text else None


def pick_letra(resultados: list[dict], duracion_ms: int = 0) -> Letra | None:
    """La mejor letra de una respuesta de lrclib: con tiempos si hay, y si no, sin ellos.

    Entre varias con tiempos gana la de duración más cercana a `duracion_ms` (si se
    conoce): un directo o una remasterización suenan desfasados respecto al tema.
    """
    sincronizadas = [(item, parse_lrc(item.get("syncedLyrics") or "")) for item in resultados]
    sincronizadas = [(item, lineas) for item, lineas in sincronizadas if lineas]
    if sincronizadas:
        def distancia(par):
            duracion = par[0].get("duration")
            return abs(duracion * 1000 - duracion_ms) if duracion_ms and duracion else float("inf")
        return Letra(True, min(sincronizadas, key=distancia)[1])
    for item in resultados:
        if letra := plain_letra(item.get("plainLyrics") or ""):
            return letra
    return None


def _busquedas(title: str, artist: str, song: str) -> list[dict]:
    busquedas = [{"track_name": song}]
    if artist:
        busquedas.insert(0, {"track_name": song, "artist_name": artist})
    busquedas.extend({"q": term} for term in search_terms(title, artist, song))
    return busquedas


async def _lrclib(session: aiohttp.ClientSession, params: dict, timeout: aiohttp.ClientTimeout) -> list[dict]:
    """Lo que lrclib responde a una búsqueda; vacío si falla."""
    log.debug("buscando %s", params)
    try:
        async with session.get("https://lrclib.net/api/search", params=params, timeout=timeout) as resp:
            if resp.status == 200:
                datos = await resp.json()
                return [item for item in datos if isinstance(item, dict)] if isinstance(datos, list) else []
            log.debug("lrclib status %s para %s", resp.status, params)
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        log.debug("lrclib falló %s: %r", params, error)
    return []


async def _ovh(session: aiohttp.ClientSession, artist: str, song: str, timeout: aiohttp.ClientTimeout) -> str | None:
    """La letra (sin tiempos) de lyrics.ovh; necesita artista."""
    if not artist:
        return None
    log.debug("fallback lyrics.ovh %s - %s", artist, song)
    url = f"https://api.lyrics.ovh/v1/{quote(artist, safe='')}/{quote(song, safe='')}"
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                return clean_lyrics_text(data.get("lyrics", "")) or None
            log.debug("lyrics.ovh status %s para %s - %s", resp.status, artist, song)
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        log.debug("lyrics.ovh falló: %r", error)
    return None


async def find(session: aiohttp.ClientSession, title: str, artist: str, song: str) -> str | None:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    for params in _busquedas(title, artist, song):
        for item in await _lrclib(session, params, timeout):
            text = clean_lyrics_text(item.get("plainLyrics") or item.get("syncedLyrics") or "")
            if text:
                return text
    return await _ovh(session, artist, song, timeout)


async def find_synced(
    session: aiohttp.ClientSession, title: str, artist: str, song: str, duracion_ms: int = 0
) -> Letra | None:
    """Como `find`, pero prefiere la letra con tiempos y la devuelve parseada.

    Se queda con la primera búsqueda que dé algo, la más precisa: seguir hasta
    encontrar tiempos arriesgaría una canción distinta.
    """
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    for params in _busquedas(title, artist, song):
        if letra := pick_letra(await _lrclib(session, params, timeout), duracion_ms):
            return letra
    return plain_letra(await _ovh(session, artist, song, timeout) or "")


class Letras:
    """Caché acotada (la menos usada sale primero) de letras por canción.

    Lo que no se encontró también se guarda, pero caduca: puede haber sido la red y
    no la canción. Las búsquedas simultáneas de una misma canción comparten una sola.
    """

    def __init__(self, maximo: int = CACHE_MAXIMA, caduca_vacia: float = CACHE_VACIA_S, reloj=time.monotonic):
        self._datos: OrderedDict[str, tuple[Letra | None, float]] = OrderedDict()
        self._en_vuelo: dict[str, asyncio.Task] = {}
        self._maximo, self._caduca, self._reloj = maximo, caduca_vacia, reloj

    async def obtener(self, clave: str, buscar: Callable[[], Awaitable[Letra | None]]) -> Letra | None:
        """La letra de `clave`: de la caché, o la que traiga `buscar()` (con tope de `TOTAL_TIMEOUT`)."""
        guardada = self._datos.get(clave)
        if guardada and (guardada[0] or self._reloj() - guardada[1] < self._caduca):
            self._datos.move_to_end(clave)
            return guardada[0]
        if clave not in self._en_vuelo:
            self._en_vuelo[clave] = asyncio.ensure_future(self._buscar(clave, buscar))
        return await asyncio.shield(self._en_vuelo[clave])

    async def _buscar(self, clave: str, buscar: Callable[[], Awaitable[Letra | None]]) -> Letra | None:
        try:
            try:
                letra = await asyncio.wait_for(buscar(), TOTAL_TIMEOUT)
            except asyncio.TimeoutError:
                letra = None
            self._datos[clave] = (letra, self._reloj())
            self._datos.move_to_end(clave)
            while len(self._datos) > self._maximo:
                self._datos.popitem(last=False)
            return letra
        finally:
            del self._en_vuelo[clave]
