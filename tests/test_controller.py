"""Dobles del wavelink.Player: propiedades (no métodos) y corutinas, como las usa el controller."""

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
        self.playing, self.paused, self.volume = playing, paused, volume
        self.queue = FakeQueue(temas)
        self.saltado = False

    async def pause(self, valor: bool):
        self.paused = valor
        self.playing = not valor

    async def skip(self, force: bool = True):
        self.saltado = True

    async def set_volume(self, valor: int):
        self.volume = valor


async def test_pause_y_resume():
    vc = FakePlayer(playing=True)
    assert "Pausado" in await controller.pause(vc)
    assert vc.paused
    assert "Reanudado" in await controller.resume(vc)
    assert vc.playing


@pytest.mark.parametrize("vc", [FakePlayer(), None], ids=["parado", "sin_player"])
async def test_pause_sin_nada_sonando(vc):
    assert "No hay nada" in await controller.pause(vc)


async def test_toggle_pause_va_y_vuelve():
    vc = FakePlayer(playing=True)
    await controller.toggle_pause(vc)
    assert vc.paused
    await controller.toggle_pause(vc)
    assert vc.playing


async def test_skip_manda_el_salto_al_nodo_solo_si_suena_algo():
    vc = FakePlayer()
    await controller.skip(vc)
    assert vc.saltado is False
    vc.playing = True
    assert "Skipped" in await controller.skip(vc)
    assert vc.saltado


async def test_toggle_loop_va_y_vuelve_y_pide_reproductor():
    vc = FakePlayer()
    assert "activado" in await controller.toggle_loop(vc)
    assert vc.queue.mode is wavelink.QueueMode.loop
    assert "desactivado" in await controller.toggle_loop(vc)
    assert vc.queue.mode is wavelink.QueueMode.normal
    assert "No hay reproductor" in await controller.toggle_loop(None)


@pytest.mark.parametrize("temas, aviso, mezclada", [([], "vacía", False), (["a", "b"], "mezclada", True)])
def test_shuffle(temas, aviso, mezclada):
    vc = FakePlayer(temas=temas)
    assert aviso in controller.shuffle(vc)
    assert vc.queue.mezclada is mezclada


async def test_volumen_llega_al_player_y_fuera_de_rango_no_toca_nada():
    vc = FakePlayer()
    assert "80%" in await controller.set_volume(vc, 80)
    assert vc.volume == 80
    assert "entre 0 y 100" in await controller.set_volume(vc, 150)
    assert vc.volume == 80


async def test_nudge_no_se_pasa_de_los_bordes():
    vc = FakePlayer(volume=95)
    await controller.nudge_volume(vc, 10)
    assert vc.volume == 100
    vc.volume = 5
    await controller.nudge_volume(vc, -10)
    assert vc.volume == 0
