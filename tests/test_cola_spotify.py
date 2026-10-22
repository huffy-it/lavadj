"""La carga de una lista de Spotify: cada nombre se busca en YouTube y se encola."""

import asyncio

import pytest
import wavelink

from core import cola_spotify
from core.cola_spotify import LOTE, Carga, buscar_uno, cargar


class FakeTrack:
    def __init__(self, title):
        self.title = title


class FakeQueue(list):
    async def put_wait(self, track):
        self.append(track)


class FakePlayer:
    def __init__(self):
        self.connected = True
        self.queue = FakeQueue()


def _playlist(*titulos):
    lista = wavelink.Playlist.__new__(wavelink.Playlist)
    lista.name = "una lista"
    lista.tracks = [FakeTrack(t) for t in titulos]
    return lista


@pytest.fixture
def buscador(monkeypatch):
    """`wavelink.Playable.search` contestando por consulta: un título, `None` (nada) o una excepción."""
    pedidas = []
    respuestas = {}

    async def _search(consulta, *a, **kw):
        pedidas.append(consulta)
        await asyncio.sleep(0)
        respuesta = respuestas.get(consulta, [FakeTrack(consulta)])
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    monkeypatch.setattr(cola_spotify.wavelink.Playable, "search", _search)
    return pedidas, respuestas


def _consultas(n):
    return [f"tema {i}" for i in range(n)]


async def _sin_arrancar(vc):
    return None


def _siempre_cabe(vc):
    return 100


# --- buscar_uno --------------------------------------------------------------------------

async def test_buscar_uno_devuelve_el_primero_y_anota_quien_lo_pidio(buscador):
    track = await buscar_uno("tema 0", "ana")

    assert track.title == "tema 0"
    assert track.requester == "ana"


@pytest.mark.parametrize("respuesta", [[], None], ids=["vacio", "none"])
async def test_buscar_uno_sin_resultados_devuelve_none(buscador, respuesta):
    buscador[1]["nada"] = respuesta

    assert await buscar_uno("nada", "ana") is None


async def test_buscar_uno_no_se_queda_con_una_playlist(buscador):
    buscador[1]["lista"] = _playlist("a", "b")

    assert await buscar_uno("lista", "ana") is None


async def test_buscar_uno_si_la_busqueda_revienta_devuelve_none_y_lo_registra(buscador, caplog):
    buscador[1]["roto"] = RuntimeError("Lavalink no responde")

    with caplog.at_level("WARNING", logger="music"):
        assert await buscar_uno("roto", "ana") is None

    assert "falló la búsqueda de 'roto'" in caplog.text


# --- cargar --------------------------------------------------------------------------------

async def test_cargar_encola_en_el_orden_de_la_lista_aunque_busque_por_lotes(buscador):
    vc = FakePlayer()
    consultas = _consultas(LOTE * 2 + 2)

    carga = await cargar(vc, consultas, "ana", _siempre_cabe, _sin_arrancar)

    assert [t.title for t in vc.queue] == consultas
    assert all(t.requester == "ana" for t in vc.queue)
    assert carga == Carga(encolados=len(consultas), perdidos=0)


async def test_cargar_pone_a_sonar_despues_de_cada_lote(buscador):
    vc = FakePlayer()
    arranques = []

    async def _arrancar(player):
        arranques.append(len(player.queue))

    await cargar(vc, _consultas(LOTE * 2 + 1), "ana", _siempre_cabe, _arrancar)

    assert arranques == [LOTE, LOTE * 2, LOTE * 2 + 1]


async def test_cargar_sin_nada_que_cargar_no_busca_ni_arranca(buscador):
    vc = FakePlayer()

    async def _arrancar(player):
        raise AssertionError("no había nada que arrancar")

    carga = await cargar(vc, [], "ana", _siempre_cabe, _arrancar)

    assert carga == Carga()
    assert buscador[0] == []


async def test_cargar_cuenta_los_temas_que_no_aparecen_en_youtube(buscador):
    vc = FakePlayer()
    buscador[1]["tema 1"] = []
    buscador[1]["tema 3"] = _playlist("x")

    carga = await cargar(vc, _consultas(5), "ana", _siempre_cabe, _sin_arrancar)

    assert [t.title for t in vc.queue] == ["tema 0", "tema 2", "tema 4"]
    assert carga == Carga(encolados=3, perdidos=2)


async def test_cargar_respeta_el_tope_de_la_cola_sin_contar_lo_que_no_cabe(buscador):
    vc = FakePlayer()
    cabe = lambda player: max(0, 3 - len(player.queue))

    carga = await cargar(vc, _consultas(6), "ana", cabe, _sin_arrancar)

    assert [t.title for t in vc.queue] == ["tema 0", "tema 1", "tema 2"]
    assert carga == Carga(encolados=3, perdidos=0)


async def test_si_el_player_se_va_a_medias_devuelve_none_y_no_sigue(buscador):
    vc = FakePlayer()

    async def _arrancar(player):
        player.connected = False

    carga = await cargar(vc, _consultas(LOTE * 3), "ana", _siempre_cabe, _arrancar)

    assert carga is None
    assert len(vc.queue) == LOTE
    assert len(buscador[0]) == LOTE


async def test_si_el_player_ya_se_fue_no_busca_nada(buscador):
    vc = FakePlayer()
    vc.connected = False

    assert await cargar(vc, _consultas(3), "ana", _siempre_cabe, _sin_arrancar) is None
    assert buscador[0] == []


async def test_un_fallo_inesperado_se_registra_y_devuelve_lo_cargado_hasta_ahi(buscador, caplog):
    vc = FakePlayer()
    llamadas = []

    async def _arrancar(player):
        llamadas.append(1)
        if len(llamadas) == 2:
            raise RuntimeError("se cayó")

    with caplog.at_level("ERROR", logger="music"):
        carga = await cargar(vc, _consultas(LOTE * 3), "ana", _siempre_cabe, _arrancar)

    assert carga == Carga(encolados=LOTE * 2, perdidos=0)
    assert "se cayó la carga de la lista de Spotify" in caplog.text


async def test_cancelar_la_carga_no_se_traga_la_cancelacion(buscador):
    vc = FakePlayer()
    frenada = asyncio.Event()

    async def _arrancar(player):
        frenada.set()
        await asyncio.sleep(3600)

    tarea = asyncio.create_task(cargar(vc, _consultas(LOTE * 2), "ana", _siempre_cabe, _arrancar))
    await frenada.wait()
    tarea.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tarea
