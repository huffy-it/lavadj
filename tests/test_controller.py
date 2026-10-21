"""El controller ahora le habla a un wavelink.Player, que es asíncrono.

El doble imita lo que el controller le pide de verdad al player: propiedades
(no métodos is_playing()) y corutinas para pausar, saltar y subir el volumen.
"""

import pytest
import wavelink

from core import controller


class FakeQueue(list):
    def __init__(self, temas=()):
        super().__init__(temas)
        self.mode = wavelink.QueueMode.normal
        self.mezclada = False

    def shuffle(self):
        self.mezclada = True


class FakePlayer:
    def __init__(self, playing=False, paused=False, volume=50, temas=()):
        self.playing = playing
        self.paused = paused
        self.volume = volume
        self.queue = FakeQueue(temas)
        self.saltado = False

    async def pause(self, valor: bool):
        self.paused = valor
        self.playing = not valor

    async def skip(self, force: bool = True):
        self.saltado = True

    async def set_volume(self, valor: int):
        self.volume = valor


@pytest.mark.asyncio
async def test_pause_y_resume():
    vc = FakePlayer(playing=True)
    assert "Pausado" in await controller.pause(vc)
    assert vc.paused
    assert "Reanudado" in await controller.resume(vc)
    assert vc.playing


@pytest.mark.asyncio
async def test_pause_sin_nada_sonando():
    assert "No hay nada" in await controller.pause(FakePlayer())


@pytest.mark.asyncio
async def test_pause_sin_reproductor():
    assert "No hay nada" in await controller.pause(None)


@pytest.mark.asyncio
async def test_toggle_pause_va_y_vuelve():
    vc = FakePlayer(playing=True)
    await controller.toggle_pause(vc)
    assert vc.paused
    await controller.toggle_pause(vc)
    assert vc.playing


@pytest.mark.asyncio
async def test_skip_manda_el_salto_al_nodo():
    vc = FakePlayer(playing=True)
    assert "Skipped" in await controller.skip(vc)
    assert vc.saltado


@pytest.mark.asyncio
async def test_skip_sin_nada_sonando_no_hace_nada():
    vc = FakePlayer()
    await controller.skip(vc)
    assert vc.saltado is False


@pytest.mark.asyncio
async def test_toggle_loop_va_y_vuelve():
    vc = FakePlayer()
    assert "activado" in await controller.toggle_loop(vc)
    assert vc.queue.mode is wavelink.QueueMode.loop
    assert "desactivado" in await controller.toggle_loop(vc)
    assert vc.queue.mode is wavelink.QueueMode.normal


@pytest.mark.asyncio
async def test_toggle_loop_sin_reproductor():
    assert "No hay reproductor" in await controller.toggle_loop(None)


def test_shuffle_con_cola_vacia():
    vc = FakePlayer()
    assert "vacía" in controller.shuffle(vc)
    assert vc.queue.mezclada is False


def test_shuffle_mezcla_la_cola():
    vc = FakePlayer(temas=["a", "b"])
    assert "mezclada" in controller.shuffle(vc)
    assert vc.queue.mezclada


@pytest.mark.asyncio
async def test_volumen_llega_al_player():
    vc = FakePlayer()
    assert "80%" in await controller.set_volume(vc, 80)
    assert vc.volume == 80


@pytest.mark.asyncio
async def test_volumen_fuera_de_rango_no_toca_nada():
    vc = FakePlayer(volume=50)
    assert "entre 0 y 100" in await controller.set_volume(vc, 150)
    assert vc.volume == 50


@pytest.mark.asyncio
async def test_nudge_no_se_pasa_de_los_bordes():
    vc = FakePlayer(volume=95)
    await controller.nudge_volume(vc, 10)
    assert vc.volume == 100
    vc.volume = 5
    await controller.nudge_volume(vc, -10)
    assert vc.volume == 0
