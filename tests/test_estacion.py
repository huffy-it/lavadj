"""El estado de un servidor en un solo objeto: se crea al usarlo y `teardown` lo tira entero."""

import asyncio

import pytest

from cogs.music import Music
from core.estacion import Estacion


class FakeGuild:
    def __init__(self, guild_id=1):
        self.id = guild_id
        self.voice_client = None


class FakeQueue(list):
    def get(self):
        return self.pop(0)


class FakePlayer:
    def __init__(self, guild=None, temas=()):
        self.guild = guild or FakeGuild()
        self.guild.voice_client = self
        self.queue = FakeQueue(temas)
        self.auto_queue = FakeQueue()
        self.connected = True
        self.playing = False
        self.paused = False

    async def disconnect(self):
        self.connected = False


class FakeCanal:
    def __init__(self):
        self.enviados = []

    async def send(self, contenido=None, **kwargs):
        self.enviados.append(contenido)


class FakeMensaje:
    def __init__(self):
        self.channel = FakeCanal()
        self.borrado = False

    async def delete(self):
        self.borrado = True


class FakeVista:
    def __init__(self):
        self.detenida = False

    def stop(self):
        self.detenida = True


@pytest.fixture
def cog():
    cog = Music(bot=None)
    cog.dj.enabled = False
    return cog


async def _terminadas(*tareas):
    """Espera a que terminen; con tope, para que una que no se canceló falle en vez de colgar."""
    await asyncio.wait_for(asyncio.gather(*tareas, return_exceptions=True), timeout=1)


async def _tarea_eterna():
    await asyncio.sleep(3600)


async def _tarea_en_marcha():
    tarea = asyncio.create_task(_tarea_eterna())
    await asyncio.sleep(0)
    return tarea


async def _estacion_cargada(cog, guild_id=1):
    """Una estación con de todo: tareas en vuelo, panel, reintentos y caídas."""
    est = cog.estacion(guild_id)
    est.idle = await _tarea_en_marcha()
    est.spotify = await _tarea_en_marcha()
    est.panel = (FakeMensaje(), FakeVista())
    est.reintentos["una-cancion"] = 1
    est.caidas = 2
    est.atascado = True
    return est


# --- la estación -----------------------------------------------------------------------

def test_una_estacion_nueva_arranca_vacia():
    est = Estacion()
    assert est.idle is None and est.spotify is None and est.panel is None
    assert est.reintentos == {} and est.caidas == 0 and est.atascado is False
    assert not est.arranque.locked()


def test_dos_estaciones_no_comparten_colecciones_ni_lock():
    a, b = Estacion(), Estacion()
    a.reintentos["x"] = 1
    assert b.reintentos == {}
    assert a.arranque is not b.arranque


async def test_cancelar_idle_corta_la_tarea_y_la_suelta():
    est = Estacion()
    est.idle = tarea = await _tarea_en_marcha()
    est.cancelar_idle()
    assert est.idle is None
    await _terminadas(tarea)
    assert tarea.cancelled()


async def test_cancelar_spotify_no_toca_la_desconexion_por_ocio():
    est = Estacion()
    est.idle = ocio = await _tarea_en_marcha()
    est.spotify = carga = await _tarea_en_marcha()
    est.cancelar_spotify()
    assert est.spotify is None and est.idle is ocio
    await _terminadas(carga)
    assert carga.cancelled() and not ocio.cancelled()
    ocio.cancel()


async def test_cancelar_tareas_corta_las_dos():
    est = Estacion()
    est.idle = ocio = await _tarea_en_marcha()
    est.spotify = carga = await _tarea_en_marcha()
    est.cancelar_tareas()
    await _terminadas(ocio, carga)
    assert ocio.cancelled() and carga.cancelled()
    assert est.idle is None and est.spotify is None


def test_cancelar_sin_tareas_no_hace_nada():
    est = Estacion()
    est.cancelar_tareas()
    assert est.idle is None and est.spotify is None


# --- el cog guarda una por servidor -------------------------------------------------

def test_el_cog_crea_la_estacion_la_primera_vez_y_despues_devuelve_la_misma(cog):
    assert cog._estaciones == {}
    est = cog.estacion(7)
    assert cog.estacion(7) is est
    assert cog._estaciones == {7: est}


def test_cada_servidor_tiene_la_suya(cog):
    assert cog.estacion(1) is not cog.estacion(2)


# --- teardown deja todo limpio ---------------------------------------------------------

async def test_teardown_deja_todo_limpio(cog):
    vc = FakePlayer(temas=[object()])
    est = await _estacion_cargada(cog)
    idle, spotify = est.idle, est.spotify
    mensaje, vista = est.panel

    await cog.teardown(vc.guild)

    assert 1 not in cog._estaciones
    await _terminadas(idle, spotify)
    assert idle.cancelled() and spotify.cancelled()
    assert mensaje.borrado and vista.detenida
    assert not vc.connected and not vc.queue
    # Lo que se pidiera después arranca de cero.
    nueva = cog.estacion(1)
    assert nueva is not est
    assert nueva.reintentos == {} and nueva.caidas == 0 and nueva.panel is None


async def test_teardown_con_nota_avisa_donde_estaba_el_panel(cog):
    vc = FakePlayer()
    est = await _estacion_cargada(cog)
    mensaje = est.panel[0]

    await cog.teardown(vc.guild, note="Parado 🛑")

    assert mensaje.channel.enviados == ["Parado 🛑"]
    assert 1 not in cog._estaciones


async def test_teardown_con_nota_pero_sin_panel_no_avisa_a_nadie(cog):
    vc = FakePlayer()
    vc.text_channel = FakeCanal()
    cog.estacion(1)

    await cog.teardown(vc.guild, note="Parado 🛑")

    assert vc.text_channel.enviados == []
    assert 1 not in cog._estaciones


async def test_teardown_de_un_servidor_sin_estacion_no_deja_ninguna(cog):
    vc = FakePlayer()

    await cog.teardown(vc.guild)

    assert cog._estaciones == {}
    assert not vc.connected


async def test_teardown_sin_player_tambien_limpia(cog):
    guild = FakeGuild()
    est = await _estacion_cargada(cog)
    mensaje = est.panel[0]

    await cog.teardown(guild)

    assert cog._estaciones == {}
    assert mensaje.borrado


async def test_teardown_no_toca_a_los_otros_servidores(cog):
    vc = FakePlayer(guild=FakeGuild(1))
    await _estacion_cargada(cog, 1)
    otra = await _estacion_cargada(cog, 2)

    await cog.teardown(vc.guild)

    assert cog._estaciones == {2: otra}
    assert not otra.idle.cancelled() and not otra.spotify.cancelled()
    assert otra.panel is not None and otra.reintentos == {"una-cancion": 1} and otra.caidas == 2
    otra.cancelar_tareas()


async def test_teardown_le_avisa_a_la_salud_y_al_dj(cog, monkeypatch):
    soltados, descartados = [], []
    vc = FakePlayer()
    monkeypatch.setattr(cog.dj, "drop", soltados.append)
    monkeypatch.setattr(cog.salud, "sin_siguiente", descartados.append)

    await cog.teardown(vc.guild)

    assert soltados == [1] and descartados == [1]


async def test_el_autoplay_es_una_preferencia_y_sobrevive_al_teardown(cog):
    """Un `!autoplay off` no se pierde al parar el bot ni al irse del canal."""
    vc = FakePlayer()
    cog._autoplay[1] = False

    await cog.teardown(vc.guild)

    assert cog.autoplay_activo(1) is False


async def test_cog_unload_corta_las_tareas_y_las_vistas_de_todos_los_servidores(cog):
    uno = await _estacion_cargada(cog, 1)
    otro = await _estacion_cargada(cog, 2)
    tareas = [uno.idle, uno.spotify, otro.idle, otro.spotify]
    vistas = [uno.panel[1], otro.panel[1]]

    await cog.cog_unload()

    await _terminadas(*tareas)
    assert all(t.cancelled() for t in tareas)
    assert all(v.detenida for v in vistas)


# --- el panel de la estación ------------------------------------------------------------

async def test_borrar_el_panel_lo_saca_de_la_estacion(cog):
    est = await _estacion_cargada(cog)
    mensaje, vista = est.panel

    await cog._delete_panel(1)

    assert est.panel is None
    assert mensaje.borrado and vista.detenida
    est.cancelar_tareas()


async def test_borrar_un_panel_que_no_existe_no_falla(cog):
    await cog._delete_panel(99)

    assert cog.estacion(99).panel is None


# --- la marca del vigilante ---------------------------------------------------------------

async def test_la_pasada_deja_en_falso_las_estaciones_que_no_vio(cog):
    visto, ausente = cog.estacion(1), cog.estacion(2)
    visto.atascado = ausente.atascado = True
    vc = FakePlayer(guild=FakeGuild(1), temas=[object()])

    await cog._vigilar_una_vez([vc])

    assert ausente.atascado is False


async def test_la_marca_se_pone_a_la_primera_y_se_suelta_al_actuar(cog):
    primero, segundo = object(), object()

    class Mudo(FakePlayer):
        async def play(self, track):
            pass

    vc = Mudo(temas=[primero, segundo])

    await cog._vigilar_una_vez([vc])
    assert cog.estacion(1).atascado is True

    await cog._vigilar_una_vez([vc])
    assert cog.estacion(1).atascado is False


async def test_si_el_servidor_deja_de_estar_atascado_pierde_la_marca(cog):
    vc = FakePlayer(temas=[object()])
    await cog._vigilar_una_vez([vc])
    assert cog.estacion(1).atascado is True

    vc.playing = True
    await cog._vigilar_una_vez([vc])

    assert cog.estacion(1).atascado is False
