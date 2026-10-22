"""La carga de una lista de Spotify: cada nombre se busca en YouTube y se encola."""

import asyncio
import logging
from dataclasses import dataclass

import wavelink

log = logging.getLogger("music")

# Temas que se buscan a la vez en YouTube: de a uno una lista de 50 tarda una
# eternidad y todos juntos es una avalancha para Lavalink.
LOTE = 5


@dataclass
class Carga:
    encolados: int = 0
    perdidos: int = 0


async def buscar_uno(consulta: str, pedido_por: str) -> wavelink.Playable | None:
    """Un nombre de Spotify → el tema en YouTube. None si no aparece."""
    try:
        tracks: wavelink.Search = await wavelink.Playable.search(consulta)
    except Exception:
        log.warning("falló la búsqueda de %r", consulta, exc_info=True)
        return None
    if not tracks or isinstance(tracks, wavelink.Playlist):
        return None
    track = tracks[0]
    track.requester = pedido_por
    return track


async def cargar(
    vc: wavelink.Player, consultas: list[str], pedido_por: str, espacio, arrancar
) -> Carga | None:
    """Busca y encola `consultas` por lotes, en orden. None si el player se fue a medias.

    `espacio(vc)` dice cuánto cabe en la cola y `arrancar(vc)` la pone a sonar si estaba parada.
    Un fallo inesperado se registra y devuelve lo cargado hasta ahí.
    """
    carga = Carga()
    try:
        for i in range(0, len(consultas), LOTE):
            if not vc.connected:
                return None
            lote = consultas[i:i + LOTE]
            # En lote pero manteniendo el orden del disco/playlist.
            resultados = await asyncio.gather(*(buscar_uno(c, pedido_por) for c in lote))
            for track in resultados:
                if track is None:
                    carga.perdidos += 1
                elif espacio(vc):
                    await vc.queue.put_wait(track)
                    carga.encolados += 1
            await arrancar(vc)
    except Exception:
        log.exception("se cayó la carga de la lista de Spotify")
    return carga
