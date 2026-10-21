"""Titulares de RSS para el DJ.

Todo vive en RAM con TTL: al reiniciar el bot los titulares desaparecen por
construcción, así que no hay caché en disco que crezca ni que haya que limpiar.

Las fuentes salen de dj_feeds.json, editable sin tocar código.
"""

import asyncio
import logging
import math
import random
import re
import weakref
from collections import deque
from pathlib import Path

import aiohttp
import feedparser

log = logging.getLogger("feeds")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = PROJECT_ROOT / "dj_feeds.json"

TTL = 900              # 15 min
FETCH_TIMEOUT = 5      # por feed
MAX_HEADLINES = 40
RECENT_MEMORY = 20     # cuántos titulares recordar para no repetir

# "Nunca se bajó". No es 0.0: el reloj es `loop.time()`, o sea monotónico desde
# el arranque de la máquina, así que recién reiniciada 0.0 queda a segundos de
# ahora y el TTL lo daría por fresco.
NEVER = -math.inf

# Los titulares traen de todo: comillas raras, guiones largos, el nombre del
# medio pegado al final. espeak lee eso pésimo.
TRAILING_SOURCE_RE = re.compile(r"\s*[-|–—]\s*[A-ZÁÉÍÓÚÑ][\w\s.]{2,30}$")
WHITESPACE_RE = re.compile(r"\s+")

# Cachés vivas, para poder vencerlas todas desde afuera: es lo que hace el
# botón "Refrescar noticias" del panel (POST /api/refresh_news) y el comando
# !refresh. WeakSet: si una caché queda sin dueño no se la retiene por estar aquí.
_INSTANCES: "weakref.WeakSet[FeedCache]" = weakref.WeakSet()


def invalidate_all() -> int:
    """Vence los titulares de todas las cachés. Devuelve cuántas tocó."""
    caches = list(_INSTANCES)
    for cache in caches:
        cache.invalidate()
    return len(caches)


async def refresh_all(session: "aiohttp.ClientSession") -> int:
    """Vence y vuelve a bajar, ya. Devuelve cuántas cachés tocó.

    Vencer solo alcanzaba cuando los titulares únicamente salían al aire: el
    próximo corte del DJ los rebajaba. Ahora el panel los muestra, y una lista
    que no cambia al apretar "Refrescar noticias" parece un botón roto.
    """
    caches = list(_INSTANCES)
    for cache in caches:
        cache.invalidate()
    if caches:
        await asyncio.gather(
            *(cache.refresh(session) for cache in caches), return_exceptions=True
        )
    return len(caches)


def clean_headline(raw: str) -> str:
    text = WHITESPACE_RE.sub(" ", raw).strip()
    text = TRAILING_SOURCE_RE.sub("", text)
    return text.strip(" .·-–—|")


class FeedCache:
    """Titulares en memoria, refrescados cada TTL segundos."""

    def __init__(self, urls: list[str], loop=None):
        self.urls = urls
        self._headlines: list[str] = []
        self._fetched_at = NEVER
        self._recent: deque[str] = deque(maxlen=RECENT_MEMORY)
        self._lock = asyncio.Lock()
        _INSTANCES.add(self)

    def invalidate(self) -> None:
        """Vence los titulares sin borrarlos: el próximo corte del DJ rebaja.

        No se vacía la lista a propósito. Si la bajada falla, `refresh` deja lo
        que había y el DJ sigue teniendo algo que leer.
        """
        self._fetched_at = NEVER

    def snapshot(self) -> dict:
        """Foto de lo que hay en RAM, para mostrarla en el panel de escritorio.

        Marca los titulares que el DJ ya leyó hace poco (los que no va a volver
        a elegir mientras queden otros), así se ve qué falta por salir al aire.
        """
        leidos = set(self._recent)
        return {
            "titulares": [
                {"texto": titular, "leido": titular in leidos}
                for titular in self._headlines
            ],
            "edad": self.age(),
            "fuentes": len(self.urls),
        }

    def age(self) -> float | None:
        """Segundos desde la última bajada. None si nunca se bajó nada."""
        if self._fetched_at == NEVER:
            return None
        try:
            return max(0.0, self._now() - self._fetched_at)
        except RuntimeError:
            return None       # sin loop corriendo no hay reloj que consultar

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def _stale(self) -> bool:
        return not self._headlines or (self._now() - self._fetched_at) > TTL

    async def _fetch_one(self, session: aiohttp.ClientSession, url: str) -> list[str]:
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        try:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    log.debug("feed %s devolvió %s", url, resp.status)
                    return []
                body = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as error:
            log.debug("feed %s falló: %r", url, error)
            return []

        # feedparser es síncrono y puede tardar en un XML grande.
        parsed = await asyncio.to_thread(feedparser.parse, body)
        titles = []
        for entry in parsed.entries:
            title = clean_headline(getattr(entry, "title", ""))
            if 15 <= len(title) <= 160:
                titles.append(title)
        return titles

    async def refresh(self, session: aiohttp.ClientSession) -> None:
        async with self._lock:
            if not self._stale():
                return
            results = await asyncio.gather(
                *(self._fetch_one(session, url) for url in self.urls),
                return_exceptions=True,
            )
            headlines = []
            for result in results:
                if isinstance(result, list):
                    headlines.extend(result)
            if not headlines:
                log.warning("ningún feed devolvió titulares")
                # No se pisa lo que había: titulares viejos son mejores que nada.
                return
            random.shuffle(headlines)
            self._headlines = headlines[:MAX_HEADLINES]
            self._fetched_at = self._now()
            log.info("feeds: %s titulares en memoria", len(self._headlines))

    async def next_headline(self, session: aiohttp.ClientSession) -> str | None:
        """Un titular que no se haya usado hace poco. None si no hay nada."""
        await self.refresh(session)
        candidates = [h for h in self._headlines if h not in self._recent]
        if not candidates:
            candidates = self._headlines
        if not candidates:
            return None
        choice = random.choice(candidates)
        self._recent.append(choice)
        return choice
