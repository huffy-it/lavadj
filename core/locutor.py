"""El DJ frente al player: sus wavs se vuelven pistas y se ponen al frente de la cola."""

import logging

import wavelink

from core import tts
from core.dj import DJ

log = logging.getLogger("music")


def proximo(vc: wavelink.Player):
    """El tema que suena después: el de la cola, o el que puso el autoplay."""
    if vc.queue:
        return vc.queue[0]
    if vc.auto_queue:
        return vc.auto_queue[0]
    return None


class Locutor:
    def __init__(self, dj: DJ):
        self.dj = dj

    async def _clip_a_track(self, clip) -> wavelink.Playable | None:
        """Convierte el .wav del DJ en un track que Lavalink pueda reproducir.

        `source=None` es obligatorio: por defecto `search()` antepone `ytmsearch:` a lo
        que no sea una URL y buscaría la ruta del archivo en YouTube Music.
        """
        try:
            resultados = await wavelink.Playable.search(str(clip.path), source=None)
        except Exception:
            log.warning("Lavalink no pudo cargar el clip del DJ", exc_info=True)
            return None
        if not resultados or isinstance(resultados, wavelink.Playlist):
            log.warning("Lavalink no encontró el clip del DJ en %s", clip.path)
            return None
        track = resultados[0]
        track.is_dj_clip = True
        return track

    async def encolar(self, vc: wavelink.Player, para: wavelink.Playable, session) -> None:
        """Sintetiza el corte del DJ y lo deja al frente, delante de `para`.

        Va por la cola y no a mano porque el que pasa de tema en tema es el AutoPlay
        de wavelink: colarlo en el momento del cambio sería competir con él.
        """
        await self.dj.prepare(vc.guild.id, para, session)
        clip = self.dj.take(vc.guild.id, para)
        if not clip:
            return
        # Mientras se sintetizaba pudo haber un skip, un shuffle o un !stop.
        if not vc.connected or proximo(vc) is not para:
            tts.cleanup(clip.path)
            return
        track = await self._clip_a_track(clip)
        if not track:
            tts.cleanup(clip.path)
            return
        # Siempre a `queue`, aunque el tema lo haya elegido el autoplay: wavelink
        # vacía `queue` antes de `auto_queue`, así que el clip queda antes de su canción.
        vc.queue.put_at(0, track)

    async def decir(self, vc: wavelink.Player, frase: str | None) -> None:
        """Que el DJ diga algo ahora mismo, al frente de la cola.

        Es el canal que sigue vivo justo cuando falla lo que hay que avisar, y como
        el tema se cayó el player está libre. `frase` es None cuando toca callarse.
        """
        if not frase or not vc.connected:
            return
        clip = await self.dj.aviso(frase)
        if not clip:
            return
        track = await self._clip_a_track(clip)
        if track:
            vc.queue.put_at(0, track)
        else:
            tts.cleanup(clip.path)

    @staticmethod
    def es_clip(pista: wavelink.Playable) -> bool:
        """Si la pista es un wav del DJ y no una canción."""
        carpeta = str(tts.CACHE_DIR)
        return any(
            str(getattr(pista, campo, "") or "").startswith(carpeta)
            for campo in ("uri", "identifier")
        )

    def borrar_clip(self, pista: wavelink.Playable) -> None:
        """Borra el wav si la pista es un corte del DJ; una canción ni se toca."""
        if self.es_clip(pista):
            tts.cleanup(getattr(pista, "uri", None) or pista.identifier)
