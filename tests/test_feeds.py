import asyncio

import pytest

from core import feeds as feeds_mod
from core.feeds import FeedCache, clean_headline, invalidate_all, refresh_all


@pytest.fixture(autouse=True)
def solo_mis_caches():
    """`_INSTANCES` es global: sin limpiarlo, `invalidate_all`/`refresh_all` tocan cachés de otros tests y los tests salen flaky."""
    feeds_mod._INSTANCES.clear()
    yield
    feeds_mod._INSTANCES.clear()


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Diario de prueba</title>
  <item><title>Alcalde inaugura puente que no cruza ningún río</title></item>
  <item><title>Estudio revela que los estudios revelan cosas</title></item>
  <item><title>corto</title></item>
</channel></rss>
"""


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Devuelve el mismo cuerpo para cualquier URL y cuenta las llamadas."""

    def __init__(self, body=RSS, status=200):
        self.body = body
        self.status = status
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        return FakeResponse(self.body, self.status)


TITULARES = ["Alcalde inaugura puente que no cruza ningún río", "Estudio revela que los estudios revelan cosas"]
URL = ["https://feed/1"]


@pytest.mark.parametrize("raw, esperado", [
    ("Pasó algo grave - BioBioChile", "Pasó algo grave"),
    ("  mucho    aire  \n aquí ", "mucho aire aquí"),
])
def test_clean_headline_saca_el_medio_y_colapsa_espacios(raw, esperado):
    assert clean_headline(raw) == esperado


async def test_parsea_titulares_del_xml_y_descarta_los_demasiado_cortos():
    cache = FeedCache(URL)
    assert await cache.next_headline(FakeSession()) in TITULARES
    assert sorted(cache._headlines) == sorted(TITULARES)


async def test_el_ttl_evita_volver_a_bajar_y_no_repite_el_ultimo_titular():
    cache, session = FeedCache(URL), FakeSession()
    primero, segundo = await cache.next_headline(session), await cache.next_headline(session)
    assert session.calls == 1
    assert primero != segundo


async def test_todos_los_feeds_caidos_devuelve_none():
    assert await FeedCache(["https://feed/1", "https://feed/2"]).next_headline(FakeSession(body="", status=500)) is None


async def test_si_el_feed_se_cae_tras_vencer_no_se_pisan_los_titulares_viejos():
    cache = FeedCache(URL)
    await cache.refresh(FakeSession())
    previos = list(cache._headlines)

    cache.invalidate()
    await cache.refresh(FakeSession(body="", status=500))

    assert cache._headlines == previos


async def test_invalidate_all_fuerza_una_bajada_nueva_aun_con_la_maquina_recien_encendida():
    """Reloj monotónico: recién reiniciada `loop.time()` vale segundos, y con un centinela 0.0 el TTL daba todo por fresco."""
    cache, session = FeedCache(URL), FakeSession()
    cache._now = lambda: 5.0
    await cache.next_headline(session)
    assert invalidate_all() >= 1
    await cache.next_headline(session)
    assert session.calls == 2


async def test_refresh_all_ademas_de_vencer_vuelve_a_bajar():
    """El botón "Refrescar noticias" muestra la lista, así que tiene que traerla ya."""
    cache, session = FeedCache(URL), FakeSession()
    await cache.next_headline(session)
    assert await refresh_all(session) >= 1
    assert session.calls == 2


async def test_snapshot_marca_los_titulares_leidos_e_informa_la_edad():
    cache = FeedCache(URL)
    leido = await cache.next_headline(FakeSession())
    foto = cache.snapshot()

    assert len(foto["titulares"]) == 2
    assert [t["texto"] for t in foto["titulares"] if t["leido"]] == [leido]
    assert 0 <= foto["edad"] < 5


def test_snapshot_sin_bajar_nada_viene_vacio():
    foto = FeedCache(["https://feed/1", "https://feed/2"]).snapshot()
    assert foto["titulares"] == []
    assert foto["edad"] is None      # "nunca se bajó", no "recién bajado"
    assert foto["fuentes"] == 2
