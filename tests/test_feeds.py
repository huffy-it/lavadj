import asyncio

import pytest

from core import feeds as feeds_mod
from core.feeds import FeedCache, clean_headline, invalidate_all, refresh_all


@pytest.fixture(autouse=True)
def solo_mis_caches():
    """`_INSTANCES` es global y las cachés de otros tests siguen vivas ahí.

    `invalidate_all`/`refresh_all` las tocarían también, contando llamadas de más
    contra la sesión de prueba: sin esto los tests salen flaky según el orden.
    """
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


def test_clean_headline_saca_el_medio_del_final():
    assert clean_headline("Pasó algo grave - BioBioChile") == "Pasó algo grave"


def test_clean_headline_colapsa_espacios():
    assert clean_headline("  mucho    aire  \n aquí ") == "mucho aire aquí"


def test_parsea_titulares_del_xml():
    cache = FeedCache(["https://feed/1"])
    session = FakeSession()
    titular = asyncio.run(cache.next_headline(session))
    assert titular in (
        "Alcalde inaugura puente que no cruza ningún río",
        "Estudio revela que los estudios revelan cosas",
    )


def test_descarta_titulares_demasiado_cortos():
    cache = FeedCache(["https://feed/1"])
    asyncio.run(cache.refresh(FakeSession()))
    assert "corto" not in cache._headlines


def test_el_ttl_evita_volver_a_bajar():
    cache = FeedCache(["https://feed/1"])
    session = FakeSession()

    async def dos_veces():
        await cache.next_headline(session)
        await cache.next_headline(session)

    asyncio.run(dos_veces())
    assert session.calls == 1   # la segunda salió de memoria


def test_no_repite_el_ultimo_titular():
    cache = FeedCache(["https://feed/1"])
    session = FakeSession()

    async def dos_veces():
        return [await cache.next_headline(session), await cache.next_headline(session)]

    primero, segundo = asyncio.run(dos_veces())
    assert primero != segundo


def test_todos_los_feeds_caidos_devuelve_none():
    cache = FeedCache(["https://feed/1", "https://feed/2"])
    titular = asyncio.run(cache.next_headline(FakeSession(body="", status=500)))
    assert titular is None


def test_no_pisa_los_titulares_viejos_si_el_feed_se_cae():
    cache = FeedCache(["https://feed/1"])
    asyncio.run(cache.refresh(FakeSession()))
    previos = list(cache._headlines)

    cache._fetched_at = -10_000   # forzamos que esté vencido
    asyncio.run(cache.refresh(FakeSession(body="", status=500)))

    assert cache._headlines == previos


def test_invalidate_all_fuerza_una_bajada_nueva():
    """Lo que hace el botón "Refrescar noticias" del panel (POST /api/refresh_news)."""
    cache = FeedCache(["https://feed/1"])
    session = FakeSession()

    async def antes_y_despues():
        await cache.next_headline(session)
        assert invalidate_all() >= 1
        await cache.next_headline(session)

    asyncio.run(antes_y_despues())
    assert session.calls == 2   # sin invalidar habría quedado en 1


def test_invalidate_sirve_con_la_maquina_recien_encendida():
    """El reloj es monotónico: recién reiniciada, `loop.time()` vale unos segundos.

    Con un centinela 0.0 el TTL daba los titulares por frescos y el botón
    "Refrescar noticias" no bajaba nada durante los primeros 15 minutos de
    encendida la máquina.
    """
    cache = FeedCache(["https://feed/1"])
    cache._now = lambda: 5.0        # 5 segundos de uptime
    session = FakeSession()

    async def antes_y_despues():
        await cache.next_headline(session)
        cache.invalidate()
        await cache.next_headline(session)

    asyncio.run(antes_y_despues())
    assert session.calls == 2


def test_refresh_all_ademas_de_vencer_vuelve_a_bajar():
    """El botón del panel ahora muestra la lista, así que tiene que traerla ya.

    `invalidate_all` sola solo vence: los titulares nuevos no llegaban hasta el
    próximo corte del DJ, y en el panel eso se veía como un botón que no hace nada.
    """
    cache = FeedCache(["https://feed/1"])
    session = FakeSession()

    async def bajar_y_refrescar():
        await cache.next_headline(session)
        assert await refresh_all(session) >= 1

    asyncio.run(bajar_y_refrescar())
    assert session.calls == 2


# --- foto para el panel -----------------------------------------------------

def test_snapshot_marca_los_titulares_ya_leidos():
    cache = FeedCache(["https://feed/1"])
    session = FakeSession()

    async def leer_uno():
        leido = await cache.next_headline(session)
        return leido, cache.snapshot()

    leido, foto = asyncio.run(leer_uno())

    assert len(foto["titulares"]) == 2
    marcados = [t["texto"] for t in foto["titulares"] if t["leido"]]
    assert marcados == [leido]


def test_snapshot_sin_bajar_nada_viene_vacio():
    foto = FeedCache(["https://feed/1", "https://feed/2"]).snapshot()
    assert foto["titulares"] == []
    assert foto["edad"] is None      # "nunca se bajó", no "recién bajado"
    assert foto["fuentes"] == 2


def test_snapshot_informa_la_edad_de_la_bajada():
    cache = FeedCache(["https://feed/1"])

    async def bajar():
        await cache.refresh(FakeSession())
        return cache.snapshot()

    foto = asyncio.run(bajar())
    assert 0 <= foto["edad"] < 5


def test_invalidate_conserva_los_titulares_que_ya_tenia():
    cache = FeedCache(["https://feed/1"])
    asyncio.run(cache.refresh(FakeSession()))
    previos = list(cache._headlines)

    cache.invalidate()
    asyncio.run(cache.refresh(FakeSession(body="", status=500)))

    assert cache._headlines == previos
