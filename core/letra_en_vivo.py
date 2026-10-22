"""La letra en vivo: un mensaje del canal de texto que se edita línea a línea mientras suena el tema."""

import asyncio
import logging
import math
import time
from bisect import bisect_right

import discord

log = logging.getLogger("letra")

EDITAR_CADA = 1.2       # s mínimos entre dos ediciones: Discord limita las del mismo canal
ESPERA_MAX = 1.0        # tope del sueño, para notar una pausa o un salto sin esperar a la próxima línea
ESPERA_MIN = 0.1
FALLOS_MAX = 3          # ediciones seguidas rechazadas antes de dejar el tema
LIMITE = 2000           # caracteres de un mensaje de Discord
LARGO_LINEA = 200
LARGO_TITULO = 100
SIN_TIEMPOS = "🎤 Este tema no trae la letra con tiempos: usa `/lyrics`."
SIN_LETRA = "🎤 No encontré la letra de este tema."
HABLA_EL_DJ = "🎙️ Habla el DJ…"
SIN_MENCIONES = discord.AllowedMentions.none()


def indice_de(tiempos: list[int], posicion_ms: int) -> int:
    """Índice de la línea que suena en `posicion_ms`; -1 antes de la primera."""
    return bisect_right(tiempos, posicion_ms) - 1


def espera_hasta(tiempos: list[int], posicion_ms: int, indice: int) -> float:
    """Segundos hasta la próxima línea, entre `ESPERA_MIN` y `ESPERA_MAX`."""
    falta = (tiempos[indice + 1] - posicion_ms) / 1000 if indice + 1 < len(tiempos) else ESPERA_MAX
    return min(ESPERA_MAX, max(ESPERA_MIN, falta))


def _limpio(texto: str, largo: int) -> str:
    """Una sola línea, recortada y con el markdown escapado: la letra y el título vienen de afuera."""
    return discord.utils.escape_markdown(" ".join(texto.split())[:largo])


def _linea(texto: str) -> str:
    """Una línea de la letra; vacía es una pausa instrumental."""
    return _limpio(texto, LARGO_LINEA) or "♪"


def armar_texto(titulo: str, lineas: list[tuple[int, str]], indice: int) -> str:
    """El mensaje con la línea `indice` en negrita, la anterior en gris y las dos que siguen; -1 es antes de la primera."""
    partes = [f"🎤 {_limpio(titulo, LARGO_TITULO)}"]
    if indice >= 1:
        partes.append(f"-# {_linea(lineas[indice - 1][1])}")
    partes.append(f"**{_linea(lineas[indice][1]) if indice >= 0 else '♪'}**")
    partes += (_linea(texto) for _, texto in lineas[indice + 1:indice + 3])
    return "\n".join(partes)[:LIMITE]


class LetraEnVivo:
    """La letra de un servidor: si está prendida, la tarea que sigue el tema y el mensaje que va editando."""

    def __init__(self, reloj=time.monotonic, dormir=asyncio.sleep):
        self.activa = False
        self.mensaje: discord.Message | None = None
        self.tarea: asyncio.Task | None = None
        self._reloj, self._dormir = reloj, dormir

    def cancelar(self) -> None:
        if self.tarea:
            self.tarea.cancel()
            self.tarea = None

    async def detener(self) -> None:
        """Corta el seguimiento y espera a que acabe, para que no compita con lo que sigue."""
        tarea = self.tarea
        self.cancelar()
        if tarea:
            await asyncio.gather(tarea, return_exceptions=True)

    async def parar(self) -> None:
        """Corta el seguimiento y borra el mensaje; sigue prendida."""
        await self.detener()
        mensaje, self.mensaje = self.mensaje, None
        if mensaje:
            try:
                await mensaje.delete()
            except discord.HTTPException:
                log.debug("no pude borrar la letra en vivo", exc_info=True)

    async def apagar(self) -> None:
        self.activa = False
        await self.parar()

    def seguir(self, vc, pista, canal, *, titulo: str, posicion, obtener) -> None:
        """Sigue la letra de `pista` (la que suena en `vc`) en `canal`; `posicion(vc)` da los ms y `obtener()` la letra."""
        self.cancelar()
        self.tarea = asyncio.create_task(self._correr(vc, pista, canal, titulo, posicion, obtener))

    async def dj(self, canal) -> None:
        """Habla el DJ: el mensaje lo dice hasta que arranque el próximo tema."""
        await self.detener()
        try:
            await self._mostrar(canal, HABLA_EL_DJ)
        except discord.HTTPException:
            log.warning("No pude avisar en la letra en vivo que habla el DJ", exc_info=True)

    async def _mostrar(self, canal, texto: str) -> None:
        """Edita el mensaje; si no hay o lo borraron, publica uno nuevo."""
        if self.mensaje:
            try:
                await self.mensaje.edit(content=texto, allowed_mentions=SIN_MENCIONES)
                return
            except discord.NotFound:
                self.mensaje = None
        self.mensaje = await canal.send(texto, allowed_mentions=SIN_MENCIONES)

    async def _correr(self, vc, pista, canal, titulo, posicion, obtener) -> None:
        try:
            try:
                letra = await obtener()
            except Exception:  # noqa: BLE001 - sin letra el tema suena igual
                log.warning("No pude buscar la letra de %r", titulo, exc_info=True)
                letra = None
            if letra is None:
                await self._mostrar(canal, SIN_LETRA)
            elif not letra.sincronizada:
                await self._mostrar(canal, SIN_TIEMPOS)
            else:
                await self._bucle(vc, pista, canal, titulo, posicion, letra.lineas)
        except Exception:  # noqa: BLE001 - es un extra: que no se lleve por delante la reproducción
            log.warning("La letra en vivo de %r se cortó", titulo, exc_info=True)
        finally:
            if self.tarea is asyncio.current_task():
                self.tarea = None

    async def _bucle(self, vc, pista, canal, titulo, posicion, lineas) -> None:
        """Edita cuando cambia la línea y, como mucho, cada `EDITAR_CADA`: si pasan más rápido, se saltean las intermedias."""
        tiempos = [ms for ms, _ in lineas]
        mostrada, ultima, fallos = None, -math.inf, 0
        while vc.connected and vc.current is pista:
            espera = ESPERA_MAX
            if mostrada is None or not vc.paused:      # en pausa no edita, pero la primera vista sale igual
                ms = posicion(vc)
                actual = indice_de(tiempos, ms)
                tarde = EDITAR_CADA - (self._reloj() - ultima)
                if actual == mostrada:
                    espera = espera_hasta(tiempos, ms, actual)
                elif tarde > 0:
                    espera = max(ESPERA_MIN, tarde)
                else:
                    ultima = self._reloj()
                    try:
                        await self._mostrar(canal, armar_texto(titulo, lineas, actual))
                        mostrada, fallos = actual, 0
                    except discord.HTTPException:
                        fallos += 1
                        if fallos >= FALLOS_MAX:
                            log.warning("No puedo editar la letra en vivo de %r: la dejo", titulo, exc_info=True)
                            return
                    continue
            await self._dormir(espera)
