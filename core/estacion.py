"""El estado de un servidor, junto: nace al primer uso y se tira entero al desconectar."""

import asyncio
from dataclasses import dataclass, field

import discord

from core.letra_en_vivo import LetraEnVivo
from core.panel import PlayerPanel


@dataclass
class Estacion:
    idle: asyncio.Task | None = None        # desconexión por falta de música
    panel: tuple[discord.Message, PlayerPanel] | None = None
    spotify: asyncio.Task | None = None     # carga de fondo de una lista
    reintentos: dict[str, int] = field(default_factory=dict)    # id de canción -> intentos gastados
    caidas: int = 0                         # temas seguidos perdidos tras gastar sus reintentos
    arranque: asyncio.Lock = field(default_factory=asyncio.Lock)   # un arranque a la vez
    atascado: bool = False                  # la revisión anterior lo vio atascado
    segmentos: list[asyncio.Task] = field(default_factory=list)     # vigías de las partes que no son música del tema en curso
    letra: LetraEnVivo = field(default_factory=LetraEnVivo)         # la letra en vivo: si está prendida, su tarea y su mensaje

    def cancelar_idle(self) -> None:
        if self.idle:
            self.idle.cancel()
            self.idle = None

    def cancelar_spotify(self) -> None:
        if self.spotify:
            self.spotify.cancel()
            self.spotify = None

    def cancelar_segmentos(self) -> None:
        for tarea in self.segmentos:
            tarea.cancel()
        self.segmentos.clear()

    def cancelar_tareas(self) -> None:
        self.cancelar_idle()
        self.cancelar_spotify()
        self.cancelar_segmentos()
        self.letra.cancelar()
