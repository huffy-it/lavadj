"""Listas de Spotify resueltas por nombre.

Spotify no entrega audio a nadie de afuera, y Lavalink no lo reproduce. Lo que
sí sirve es la lista de compras: de cada tema sacamos "Artista - Título" y eso
se busca en YouTube, que es quien realmente suena. O sea que el link de Spotify
es un índice, no una fuente.

Los nombres salen del reproductor embebible (`open.spotify.com/embed/...`), que
es público: no hace falta registrar una app ni guardar credenciales. El precio
de esa comodidad es que la lista viene recortada — unos 50 temas — así que una
playlist gigante se encola parcial y hay que decirlo.
"""

import json
import logging
import re
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("spotify")

TIMEOUT = 10

# El embed devuelve la página del reproductor; sin User-Agent de navegador a
# veces contesta una versión sin datos.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "es,en;q=0.8",
}

# https://open.spotify.com/intl-es/playlist/xxxx?si=yyyy  y  spotify:track:xxxx
URL_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?|spotify:)"
    r"(track|album|playlist)[:/]([A-Za-z0-9]+)",
    re.IGNORECASE,
)

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


@dataclass
class Lista:
    """Lo que se pudo leer de un link de Spotify."""
    kind: str                              # track | album | playlist
    name: str
    queries: list[str] = field(default_factory=list)
    truncada: bool = False                 # el embed dio menos de lo que hay


def es_spotify(texto: str) -> bool:
    return bool(URL_RE.search(texto or ""))


def _identificar(texto: str) -> tuple[str, str] | None:
    match = URL_RE.search(texto or "")
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


def _consulta(titulo: str, artista: str) -> str:
    """El texto que se le pasa al buscador de YouTube.

    Con el artista adelante los covers y las versiones de karaoke quedan mucho
    más abajo en los resultados.
    """
    titulo = " ".join((titulo or "").split())
    artista = " ".join((artista or "").split())
    # El subtítulo trae varios artistas separados por coma; con el primero
    # alcanza y sobra para desambiguar, y ensucia menos la búsqueda.
    artista = artista.split(",")[0].strip()
    if artista and titulo:
        return f"{artista} - {titulo}"
    return titulo or artista


def _artistas(entidad: dict) -> str:
    nombres = [
        a.get("name", "")
        for a in (entidad.get("artists") or entidad.get("authors") or [])
        if isinstance(a, dict)
    ]
    return ", ".join(n for n in nombres if n)


def parse_embed(html: str, limit: int) -> Lista | None:
    """Saca nombre y temas del JSON que el embed deja incrustado en el HTML."""
    match = NEXT_DATA_RE.search(html or "")
    if not match:
        log.warning("el embed de Spotify no traía __NEXT_DATA__ (¿cambió la página?)")
        return None
    try:
        datos = json.loads(match.group(1))
        entidad = datos["props"]["pageProps"]["state"]["data"]["entity"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning("no pude leer el JSON del embed de Spotify", exc_info=True)
        return None

    temas = entidad.get("trackList")
    if not isinstance(temas, list) or not temas:
        # Un link a un tema suelto viene sin trackList: la entidad misma ES el
        # tema. Ahí el artista no está en `subtitle` (viene nulo) sino en
        # `artists`, así que hay que ir a buscarlo aparte o la búsqueda sale
        # con el título pelado y cae en cualquier cover.
        titulo = entidad.get("title") or entidad.get("name")
        if not titulo:
            return None
        temas = [{"title": titulo, "subtitle": entidad.get("subtitle") or _artistas(entidad)}]

    queries = []
    for tema in temas[:limit]:
        if not isinstance(tema, dict):
            continue
        consulta = _consulta(tema.get("title", ""), tema.get("subtitle", ""))
        if consulta:
            queries.append(consulta)

    if not queries:
        return None

    return Lista(
        kind=str(entidad.get("type") or "playlist"),
        name=str(entidad.get("name") or entidad.get("title") or "Spotify"),
        queries=queries,
        truncada=len(temas) > limit,
    )


async def fetch(session: aiohttp.ClientSession, url: str, limit: int = 100) -> Lista | None:
    """Los nombres de los temas de un link de Spotify. None si no se pudo."""
    identidad = _identificar(url)
    if not identidad:
        return None
    kind, spotify_id = identidad

    embed = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    try:
        async with session.get(embed, headers=HEADERS, timeout=timeout) as resp:
            if resp.status != 200:
                log.warning("Spotify devolvió %s para %s", resp.status, embed)
                return None
            html = await resp.text()
    except (aiohttp.ClientError, UnicodeDecodeError) as error:
        log.warning("no pude bajar %s: %r", embed, error)
        return None
    except TimeoutError:
        log.warning("Spotify no respondió a tiempo (%ss)", TIMEOUT)
        return None

    lista = parse_embed(html, limit)
    if lista:
        lista.kind = kind
    return lista
