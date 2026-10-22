"""Las partes de un videoclip que no son la canción, según SponsorBlock (`music_offtopic`).

Se pregunta con k-anonimato: al servidor solo le llegan los primeros 4 caracteres del
hash del video, y la respuesta se filtra aquí por el video exacto. Cualquier fallo da
cero segmentos: esto nunca puede frenar ni romper la reproducción.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import NamedTuple

import aiohttp

from core import config

log = logging.getLogger("sponsorblock")

API = "https://sponsor.ajay.app/api/skipSegments"
PREFIJO = 4
TIMEOUT = 2.0
CACHE_MAXIMA = 512
# Con el servicio caído cada canción esperaría el tiempo límite entero antes de sonar.
PAUSA_TRAS_ERROR = 60
INTRO_MS = 1000
CERCA_DEL_FINAL_MS = 5000
# Si el video dura distinto de cuando se marcó, es otra edición y los tiempos no valen.
DESFASE_MAXIMO_MS = 3000


class Segmento(NamedTuple):
    """Una parte que no es música, en milisegundos; `duracion` es lo que duraba el video al marcarla."""

    inicio: int
    fin: int
    duracion: int = 0


def _unir(segmentos: Iterable[Segmento]) -> tuple[Segmento, ...]:
    """Ordenados y sin solaparse."""
    unidos: list[Segmento] = []
    for segmento in sorted(segmentos):
        if unidos and segmento.inicio <= unidos[-1].fin:
            unidos[-1] = unidos[-1]._replace(fin=max(unidos[-1].fin, segmento.fin))
        else:
            unidos.append(segmento)
    return tuple(unidos)


def _leer(datos, video: str) -> tuple[Segmento, ...]:
    """Los segmentos de `video` en la respuesta por prefijo; lo mal formado se descarta."""
    if not isinstance(datos, list):
        return ()
    encontrados = []
    for entrada in datos:
        if not isinstance(entrada, dict) or entrada.get("videoID") != video:
            continue
        for crudo in entrada.get("segments") or ():
            try:
                inicio, fin = (round(float(segundos) * 1000) for segundos in crudo["segment"])
                duracion = round(float(crudo.get("videoDuration") or 0) * 1000)
            except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
                continue
            if crudo.get("actionType", "skip") == "skip" and 0 <= inicio < fin:
                encontrados.append(Segmento(inicio, fin, duracion))
    return _unir(encontrados)


def utiles(segmentos: Iterable[Segmento], largo_ms: int) -> tuple[Segmento, ...]:
    """Los que valen para una pista de `largo_ms`: sin los de otra edición ni los que se la comen entera."""
    if not largo_ms:
        return tuple(segmentos)
    return tuple(
        s for s in segmentos
        if (not s.duracion or abs(s.duracion - largo_ms) <= DESFASE_MAXIMO_MS)
        and not (s.inicio <= INTRO_MS and llega_al_final(s, largo_ms))
    )


def fin_de_intro(segmentos: Iterable[Segmento]) -> int:
    """Ms donde termina la parte que empieza con el video; 0 si no hay."""
    return next((s.fin for s in segmentos if s.inicio <= INTRO_MS), 0)


def llega_al_final(segmento: Segmento, largo_ms: int) -> bool:
    return bool(largo_ms) and largo_ms - segmento.fin <= CERCA_DEL_FINAL_MS


class SponsorBlock:
    """Consulta los segmentos de cada video y los recuerda; hay una instancia por `Resolver`.

    Los parámetros en `None` toman el valor de `core.config` al construir.
    """

    def __init__(
        self,
        *,
        activo: bool | None = None,
        categorias: tuple[str, ...] | None = None,
        timeout: float = TIMEOUT,
        tope: int = CACHE_MAXIMA,
        url: str = API,
        reloj: Callable[[], float] | None = None,
    ):
        self.activo = config.SPONSORBLOCK if activo is None else activo
        self.categorias = tuple(config.SPONSORBLOCK_CATEGORIAS if categorias is None else categorias)
        self.timeout = timeout
        self.tope = tope
        self.url = url
        self._reloj = reloj or time.monotonic
        # Por id de video; () es "se preguntó y no hay nada".
        self._cache: "OrderedDict[str, tuple[Segmento, ...]]" = OrderedDict()
        # Referencia fuerte a las consultas sueltas: el loop solo guarda débiles.
        self._en_curso: dict[str, asyncio.Task] = {}
        self._http: aiohttp.ClientSession | None = None
        self._sin_consultar_hasta = 0.0

    def en_cache(self, video: str) -> tuple[Segmento, ...] | None:
        """Lo que se sabe del video sin esperar; None si nunca se preguntó."""
        segmentos = self._cache.get(video)
        if segmentos is not None:
            self._cache.move_to_end(video)
        return segmentos

    def precargar(self, video: str) -> None:
        """Pide los segmentos en segundo plano (necesita un loop en marcha)."""
        if self.activo and video and video not in self._cache:
            self._consulta(video)

    async def segmentos(self, video: str) -> tuple[Segmento, ...]:
        """Los segmentos del video, esperando como mucho el tiempo límite; () si no hay o no se pudo."""
        if not (self.activo and video):
            return ()
        en_cache = self.en_cache(video)
        if en_cache is not None:
            return en_cache
        # shield: si quien espera se cancela, la consulta sigue y deja la respuesta en la caché.
        return await asyncio.shield(self._consulta(video))

    async def cerrar(self) -> None:
        """Corta las consultas en curso y cierra la sesión; una consulta posterior abre otra."""
        tareas = list(self._en_curso.values())
        for tarea in tareas:
            tarea.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
        sesion, self._http = self._http, None
        if sesion:
            await sesion.close()

    def _consulta(self, video: str) -> asyncio.Task:
        tarea = self._en_curso.get(video)
        if tarea is None:
            tarea = self._en_curso[video] = asyncio.create_task(self._traer(video))
            tarea.add_done_callback(lambda _: self._en_curso.pop(video, None))
        return tarea

    def _sesion(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._http

    async def _traer(self, video: str) -> tuple[Segmento, ...]:
        """Pregunta al servidor y recuerda la respuesta; si falla devuelve () y no recuerda nada."""
        if self._reloj() < self._sin_consultar_hasta:
            return ()
        prefijo = hashlib.sha256(video.encode()).hexdigest()[:PREFIJO]
        try:
            async with self._sesion().get(
                f"{self.url}/{prefijo}", params={"categories": json.dumps(list(self.categorias))}
            ) as respuesta:
                if respuesta.status == 404:
                    datos = []
                else:
                    respuesta.raise_for_status()
                    datos = await respuesta.json(content_type=None)
            segmentos = _leer(datos, video)
        except Exception as error:  # noqa: BLE001 - esto nunca puede romper la reproducción
            self._sin_consultar_hasta = self._reloj() + PAUSA_TRAS_ERROR
            log.warning("SponsorBlock no respondió (%s): sigo sin saltar partes", str(error) or type(error).__name__)
            return ()
        self._cache[video] = segmentos
        while len(self._cache) > self.tope:
            self._cache.popitem(last=False)
        return segmentos
