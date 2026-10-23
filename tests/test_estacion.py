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
    """Con tope, para que una tarea que no se canceló falle en vez de colgar."""
    await asyncio.wait_for(asyncio.gather(*tareas, return_exceptions=True), timeout=1)


async def _tarea_en_marcha():
    tarea = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.sleep(0)
    return tarea


async def _estacion_cargada(cog, guild_id=1):
    """Una estación con de todo: tareas en vuelo, vigías, precarga, letra, panel, reintentos y caídas."""
    est = cog.estacion(guild_id)
    est.idle, est.spotify, est.precarga, est.letra.tarea = [await _tarea_en_marcha() for _ in range(4)]
    est.segmentos = [await _tarea_en_marcha()]
    est.panel = (FakeMensaje(), FakeVista())
    est.letra.activa, est.letra.mensaje = True, FakeMensaje()
    est.reintentos["una-cancion"] = 1
    est.caidas = 2
    est.atascado = True
    return est


def _tareas(est):
    return [est.idle, est.spotify, est.precarga, est.letra.tarea, *est.segmentos]


def test_una_estacion_nueva_arranca_vacia_y_no_comparte_nada_con_otra():
    a, b = Estacion(), Estacion()
    assert a.idle is a.spotify is a.panel is a.precarga is None
    assert a.reintentos == {} and a.caidas == 0 and a.atascado is False and a.segmentos == []
    assert not a.arranque.locked()
    assert a.letra.activa is False and a.letra.tarea is None and a.letra.mensaje is None
    a.reintentos["x"] = 1
    a.segmentos.append(object())
    a.letra.activa = True
    assert b.reintentos == {} and b.segmentos == [] and b.letra.activa is False
    assert a.arranque is not b.arranque and a.letra is not b.letra


@pytest.mark.parametrize("metodo, cortadas", [
    ("cancelar_idle", {"idle"}),
    ("cancelar_spotify", {"spotify"}),
    ("cancelar_segmentos", {"segmentos"}),
    ("cancelar_precarga", {"precarga"}),
    ("cancelar_tareas", {"idle", "spotify", "segmentos", "precarga", "letra"}),
])
async def test_cada_cancelacion_corta_lo_suyo_sin_tocar_lo_demas(metodo, cortadas):
    est = Estacion()
    est.idle, est.spotify, est.precarga, est.letra.tarea = [await _tarea_en_marcha() for _ in range(4)]
    est.segmentos = [await _tarea_en_marcha(), await _tarea_en_marcha()]
    tareas = {"idle": [est.idle], "spotify": [est.spotify], "precarga": [est.precarga],
              "letra": [est.letra.tarea], "segmentos": list(est.segmentos)}
    getattr(est, metodo)()
    await _terminadas(*[t for nombre in cortadas for t in tareas[nombre]])
    suelto = {"idle": est.idle, "spotify": est.spotify, "precarga": est.precarga,
              "letra": est.letra.tarea, "segmentos": est.segmentos or None}
    for nombre, lista in tareas.items():
        assert all(t.cancelled() == (nombre in cortadas) for t in lista), nombre
        assert (suelto[nombre] is None) == (nombre in cortadas), nombre
    est.cancelar_tareas()


def test_cancelar_sin_tareas_no_hace_nada():
    est = Estacion()
    for metodo in ("cancelar_idle", "cancelar_spotify", "cancelar_segmentos", "cancelar_precarga", "cancelar_tareas"):
        getattr(est, metodo)()
    assert est.idle is est.spotify is est.precarga is est.letra.tarea is None and est.segmentos == []


def test_el_cog_crea_la_estacion_la_primera_vez_y_despues_devuelve_la_misma(cog):
    assert cog._estaciones == {}
    est = cog.estacion(7)
    assert cog.estacion(7) is est
    assert cog._estaciones == {7: est}
    assert cog.estacion(1) is not cog.estacion(2)


async def test_teardown_deja_todo_limpio(cog):
    vc = FakePlayer(temas=[object()])
    est = await _estacion_cargada(cog)
    tareas, (mensaje, vista), mensaje_letra = _tareas(est), est.panel, est.letra.mensaje
    await cog.teardown(vc.guild)
    assert 1 not in cog._estaciones
    await _terminadas(*tareas)
    assert all(t.cancelled() for t in tareas)
    assert mensaje.borrado and vista.detenida and mensaje_letra.borrado
    assert est.letra.activa is False and est.letra.mensaje is None
    assert not vc.connected and not vc.queue
    nueva = cog.estacion(1)
    assert nueva is not est
    assert nueva.reintentos == {} and nueva.caidas == 0 and nueva.panel is None and nueva.letra.activa is False


async def test_teardown_con_nota_avisa_donde_estaba_el_panel(cog):
    vc = FakePlayer()
    mensaje = (await _estacion_cargada(cog)).panel[0]
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
    assert cog._estaciones == {} and not vc.connected


async def test_teardown_sin_player_tambien_limpia(cog):
    guild = FakeGuild()
    mensaje = (await _estacion_cargada(cog)).panel[0]
    await cog.teardown(guild)
    assert cog._estaciones == {} and mensaje.borrado


async def test_teardown_no_toca_a_los_otros_servidores(cog):
    vc = FakePlayer(guild=FakeGuild(1))
    await _estacion_cargada(cog, 1)
    otra = await _estacion_cargada(cog, 2)
    mensaje_letra = otra.letra.mensaje
    await cog.teardown(vc.guild)
    assert cog._estaciones == {2: otra}
    assert not any(t.cancelled() for t in _tareas(otra))
    assert otra.panel is not None and otra.reintentos == {"una-cancion": 1} and otra.caidas == 2
    assert otra.letra.activa and not mensaje_letra.borrado
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
    uno, otro = await _estacion_cargada(cog, 1), await _estacion_cargada(cog, 2)
    tareas = _tareas(uno) + _tareas(otro)
    vistas = [uno.panel[1], otro.panel[1]]
    await cog.cog_unload()
    await _terminadas(*tareas)
    assert all(t.cancelled() for t in tareas)
    assert all(v.detenida for v in vistas)


async def test_borrar_el_panel_lo_saca_de_la_estacion(cog):
    est = await _estacion_cargada(cog)
    mensaje, vista = est.panel
    await cog._delete_panel(1)
    assert est.panel is None and mensaje.borrado and vista.detenida
    est.cancelar_tareas()


async def test_borrar_un_panel_que_no_existe_no_falla(cog):
    await cog._delete_panel(99)
    assert cog.estacion(99).panel is None


async def test_la_pasada_deja_en_falso_las_estaciones_que_no_vio(cog):
    visto, ausente = cog.estacion(1), cog.estacion(2)
    visto.atascado = ausente.atascado = True
    await cog._vigilar_una_vez([FakePlayer(guild=FakeGuild(1), temas=[object()])])
    assert ausente.atascado is False


async def test_la_marca_se_pone_a_la_primera_y_se_suelta_al_actuar(cog):
    class Mudo(FakePlayer):
        async def play(self, track):
            pass
    vc = Mudo(temas=[object(), object()])
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
