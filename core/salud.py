"""Cómo viene la transmisión: bien, flojo o mudo.

**Por qué existe.** El bot se puede quedar mudo sin caerse. Pasó: el proceso
vivo, `/api/status` contestando 200, el panel todo verde, y el autoplay de
wavelink frenado por errores consecutivos sin que sonara nada. Desde afuera no
había manera de notarlo.

Aquí solo se lleva el estado y se contesta cómo viene
la cosa. Quien lo usa decide qué hacer — el panel lo pinta, el DJ lo dice. Una
sola pieza de estado y dos salidas, para que no haya dos lógicas de "¿esto está
mal?" que se puedan desincronizar.

**"Mudo" se mide por tiempo, no por evento.** No existe ningún evento de "el
autoplay se rindió": wavelink lo escupe al log y no lo expone. Así que la
condición es estar al aire, con cola, y hace rato que no arranca ninguna
canción.
"""

import time

from core import config

# Fallos seguidos a partir de los cuales la cosa viene fea. Uno solo no cuenta:
# el de YouTube es intermitente y se recupera con el reintento.
FALLOS_PARA_FLOJO = 2

# Temas caídos a partir de los cuales el DJ larga un resumen. Antes de eso se
# calla: avisar uno por uno sería cuatro interrupciones por la misma desgracia.
RESUMEN_CADA = 4


class Salud:
    def __init__(self, reloj=time.monotonic):
        self._reloj = reloj
        self._fallos_seguidos = 0
        self._rendidos = 0
        self._ultimo_motivo: str | None = None
        self._ultimo_sonido = reloj()

    # --- lo que le cuenta el cog ---------------------------------------------

    def sono(self) -> None:
        """Arrancó una canción. Es la única prueba de que esto funciona."""
        self._fallos_seguidos = 0
        self._rendidos = 0
        self._ultimo_motivo = None
        self._ultimo_sonido = self._reloj()

    def fallo(self, motivo: str | None = None) -> None:
        """Una canción no pudo sonar."""
        self._fallos_seguidos += 1
        self._ultimo_motivo = motivo

    def rendido(self, titulo: str, motivo: str | None = None) -> str | None:
        """Se dio por vencido con una canción. Qué decir en voz, o None si toca callarse.

        Las reglas son las que separan un aviso útil de un loro: se habla cuando
        el bot se rinde (no en cada reintento, que es asunto interno), se dice el
        primero y después se hace silencio, y solo con varios caídos se lanza
        un resumen, una sola vez.
        """
        self._rendidos += 1

        if self._rendidos == 1:
            if motivo:
                return f"{titulo} {motivo}, la salteo."
            return f"Se me cayó {titulo}, sigo con la próxima."

        if self._rendidos == RESUMEN_CADA:
            return f"Van {RESUMEN_CADA} temas que no puedo sacar."

        return None

    # --- lo que contesta ------------------------------------------------------

    @property
    def sin_sonar(self) -> float:
        """Segundos desde la última canción que arrancó."""
        return self._reloj() - self._ultimo_sonido

    def estado(self, *, al_aire: bool, hay_cola: bool) -> dict:
        """Cómo viene la transmisión, para el panel y para la voz del DJ.

        Se informa lo más grave: quedarse sin música tapa cualquier otra cosa.
        """
        callado = int(self.sin_sonar)

        if al_aire and hay_cola and callado >= config.SILENCIO_PARA_MUDO:
            minutos = callado // 60
            return self._parte(
                "mudo", f"hace {minutos} min que no arranca ninguna canción", callado
            )

        if self._fallos_seguidos >= FALLOS_PARA_FLOJO:
            motivo = f"{self._fallos_seguidos} temas seguidos sin poder sonar"
            if self._ultimo_motivo:
                motivo = f"{motivo} ({self._ultimo_motivo})"
            return self._parte("flojo", motivo, callado)

        return self._parte("bien", None, callado)

    def _parte(self, estado: str, motivo: str | None, callado: int) -> dict:
        return {
            "estado": estado,
            "motivo": motivo,
            "fallos_seguidos": self._fallos_seguidos,
            "sin_sonar": callado,
        }
