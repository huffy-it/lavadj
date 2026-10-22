"""La API local que consume el panel de escritorio.

Los handlers se llaman directo con una request de mentira: levantar el servidor
de verdad obligaría a ocupar un puerto y no probaría nada más.
"""

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from core import api
from core.dj import DJ
from core.feeds import FeedCache
from core.salud import Salud


class FakeCog:
    def __init__(self, dj=None, session="sesión", salud=None):
        self.dj = dj if dj is not None else DJ()
        self._session = session
        self.salud = salud if salud is not None else Salud()


class FakeBot:
    def __init__(self, cog=None):
        self.cog = cog
        self.guilds = []
        self.user = "LavaDJ#7584"

    def get_cog(self, nombre):
        return self.cog

    def is_ready(self):
        return True


def pedido(metodo, ruta, bot=None):
    app = web.Application()
    app["bot"] = bot if bot is not None else FakeBot(FakeCog())
    app["apagar"] = asyncio.Event()
    app["arranque"] = 0.0
    return make_mocked_request(metodo, ruta, app=app)


def cuerpo(respuesta: web.Response) -> dict:
    return json.loads(respuesta.body)


# --- volumen del DJ ---------------------------------------------------------

def test_get_dj_volume_informa_el_actual():
    cog = FakeCog()
    cog.dj.volume = 70

    respuesta = asyncio.run(api.handle_dj_volume(pedido("GET", "/api/dj_volume", FakeBot(cog))))

    assert cuerpo(respuesta)["volumen"] == 70


def test_post_dj_volume_lo_cambia():
    cog = FakeCog()

    respuesta = asyncio.run(
        api.handle_dj_volume(pedido("POST", "/api/dj_volume?valor=45", FakeBot(cog)))
    )

    assert cuerpo(respuesta)["volumen"] == 45
    assert cog.dj.volume == 45


def test_post_dj_volume_topea_en_200():
    cog = FakeCog()

    respuesta = asyncio.run(
        api.handle_dj_volume(pedido("POST", "/api/dj_volume?valor=900", FakeBot(cog)))
    )

    assert cuerpo(respuesta)["volumen"] == 200
    assert cog.dj.volume == 200


def test_post_dj_volume_con_basura_es_400():
    cog = FakeCog()
    cog.dj.volume = 100

    respuesta = asyncio.run(
        api.handle_dj_volume(pedido("POST", "/api/dj_volume?valor=fuerte", FakeBot(cog)))
    )

    assert respuesta.status == 400
    assert cog.dj.volume == 100      # no se movió


def test_dj_volume_sin_cog_es_503():
    respuesta = asyncio.run(api.handle_dj_volume(pedido("GET", "/api/dj_volume", FakeBot(None))))
    assert respuesta.status == 503


# --- titulares --------------------------------------------------------------

def test_headlines_devuelve_la_foto_de_la_cache():
    cog = FakeCog()
    cog.dj.feeds = FeedCache(["https://feed/1"])
    cog.dj.feeds._headlines = ["Alcalde inaugura un puente", "Sube el pan"]
    cog.dj.feeds._recent.append("Sube el pan")

    respuesta = asyncio.run(api.handle_headlines(pedido("GET", "/api/headlines", FakeBot(cog))))

    datos = cuerpo(respuesta)
    assert [t["texto"] for t in datos["titulares"]] == [
        "Alcalde inaugura un puente", "Sube el pan",
    ]
    assert [t["leido"] for t in datos["titulares"]] == [False, True]


def test_headlines_sin_feeds_configurados_no_es_un_error():
    """Sin dj_feeds.json el DJ no tiene caché: el panel lo dice, no revienta."""
    cog = FakeCog()
    cog.dj.feeds = None

    respuesta = asyncio.run(api.handle_headlines(pedido("GET", "/api/headlines", FakeBot(cog))))

    assert respuesta.status == 200
    datos = cuerpo(respuesta)
    assert datos["titulares"] == []
    assert datos["fuentes"] == 0


# --- refrescar --------------------------------------------------------------

def test_refresh_baja_de_nuevo_y_devuelve_los_titulares(monkeypatch):
    cog = FakeCog()
    cog.dj.feeds = FeedCache(["https://feed/1"])
    bajadas = []

    async def falso_refresh_all(session):
        bajadas.append(session)
        cog.dj.feeds._headlines = ["Titular recién bajado"]
        return 1

    monkeypatch.setattr(api, "refresh_feeds", falso_refresh_all)

    respuesta = asyncio.run(api.handle_refresh(pedido("POST", "/api/refresh_news", FakeBot(cog))))

    assert bajadas == ["sesión"]
    datos = cuerpo(respuesta)
    assert datos["cleared"] == 1
    assert [t["texto"] for t in datos["titulares"]] == ["Titular recién bajado"]


def test_refresh_sin_sesion_igual_vence(monkeypatch):
    """Si el bot todavía no abrió su sesión HTTP, al menos se vencen los datos."""
    cog = FakeCog(session=None)
    vencidas = []
    monkeypatch.setattr(api, "invalidate_feeds", lambda: vencidas.append(True) or 1)

    respuesta = asyncio.run(api.handle_refresh(pedido("POST", "/api/refresh_news", FakeBot(cog))))

    assert respuesta.status == 200
    assert vencidas == [True]


# --- estado -----------------------------------------------------------------

async def test_el_estado_dice_como_viene_la_transmision():
    """El panel no puede notar un bot mudo si la API solo dice "running"."""
    respuesta = await api.handle_status(pedido("GET", "/api/status"))
    datos = cuerpo(respuesta)

    assert datos["salud"]["estado"] == "bien"
    assert datos["salud"]["fallos_seguidos"] == 0
    assert datos["salud"]["hueco"] is None      # todavía no hubo ninguna transición


async def test_el_estado_expone_el_hueco_entre_pistas():
    marca = {"t": 0.0}
    salud = Salud(reloj=lambda: marca["t"])
    salud.termino(1)
    marca["t"] = 2.5
    salud.arranco(1)
    bot = FakeBot(FakeCog(salud=salud))

    datos = cuerpo(await api.handle_status(pedido("GET", "/api/status", bot)))

    assert datos["salud"]["hueco"] == {
        "ultimo": 2.5, "promedio": 2.5, "maximo": 2.5, "muestras": 1,
    }


async def test_el_estado_delata_al_bot_que_viene_fallando():
    salud = Salud()
    salud.fallo()
    salud.fallo()
    bot = FakeBot(FakeCog(salud=salud))

    datos = cuerpo(await api.handle_status(pedido("GET", "/api/status", bot)))

    assert datos["salud"]["estado"] == "flojo"
    assert datos["salud"]["motivo"]


async def test_el_estado_incluye_la_antiguedad_de_yt_dlp():
    """Es el punto único de falla de toda la música y nadie lo mira."""
    datos = cuerpo(await api.handle_status(pedido("GET", "/api/status")))
    assert "ytdlp" in datos
    assert "viejo" in datos["ytdlp"]


async def test_sin_cog_cargado_el_estado_no_revienta():
    """Los dos primeros segundos del arranque el cog todavía no está."""
    datos = cuerpo(await api.handle_status(pedido("GET", "/api/status", FakeBot(None))))
    assert datos["salud"] is None


# --- panel web --------------------------------------------------------------

def test_la_pagina_del_panel_se_sirve():
    respuesta = asyncio.run(api.handle_pagina(pedido("GET", "/")))
    assert respuesta.content_type == "text/html"
    assert "LavaDJ" in respuesta.text


def test_el_log_devuelve_las_ultimas_lineas(tmp_path, monkeypatch):
    archivo = tmp_path / "bot.log"
    archivo.write_text("\n".join(f"linea {i}" for i in range(10)), encoding="utf-8")
    monkeypatch.setattr(api, "LOG_FILE", archivo)

    respuesta = asyncio.run(api.handle_log(pedido("GET", "/api/log?lineas=3")))

    assert cuerpo(respuesta)["lineas"] == ["linea 7", "linea 8", "linea 9"]


def test_el_log_sin_archivo_viene_vacio(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "LOG_FILE", tmp_path / "no_existe.log")
    respuesta = asyncio.run(api.handle_log(pedido("GET", "/api/log")))
    assert cuerpo(respuesta)["lineas"] == []


# --- protección contra CSRF y DNS rebinding --------------------------------

async def _con_servidor(prueba):
    """Levanta la app de verdad en un puerto libre y le pasa (cliente, puerto, app).

    El middleware compara contra el puerto que se le dio, así que se crea con el
    mismo puerto en el que escucha el servidor de prueba.
    """
    from aiohttp.test_utils import TestClient, TestServer, unused_port

    puerto = unused_port()
    app = api.crear_app(FakeBot(FakeCog()), asyncio.Event(), puerto)
    async with TestClient(TestServer(app, port=puerto)) as cliente:
        await prueba(cliente, puerto, app)


async def test_host_ajeno_da_403():
    async def prueba(cliente, puerto, app):
        for host in ("evil.example.com", f"evil.example.com:{puerto}", "127.0.0.1", f"127.0.0.1:{puerto + 1}"):
            r = await cliente.get("/api/dj_volume", headers={"Host": host})
            assert r.status == 403, host

    await _con_servidor(prueba)


async def test_get_con_host_bueno_funciona():
    async def prueba(cliente, puerto, app):
        for host in (f"127.0.0.1:{puerto}", f"localhost:{puerto}", f"[::1]:{puerto}"):
            r = await cliente.get("/api/dj_volume", headers={"Host": host})
            assert r.status == 200, host

    await _con_servidor(prueba)


async def test_post_con_origin_ajeno_da_403_y_no_apaga():
    async def prueba(cliente, puerto, app):
        for origen in ("http://evil.example.com", "null", f"http://127.0.0.1:{puerto + 1}", "https://127.0.0.1:%d" % puerto):
            r = await cliente.post("/api/stop", headers={"Origin": origen})
            assert r.status == 403, origen
        assert not app["apagar"].is_set()

    await _con_servidor(prueba)


async def test_post_con_origin_propio_funciona():
    async def prueba(cliente, puerto, app):
        r = await cliente.post("/api/stop", headers={"Origin": f"http://127.0.0.1:{puerto}"})
        assert r.status == 200
        assert app["apagar"].is_set()

    await _con_servidor(prueba)


async def test_post_sin_origin_funciona():
    # Así llama el panel GTK (urllib no manda Origin).
    async def prueba(cliente, puerto, app):
        r = await cliente.post("/api/stop")
        assert r.status == 200
        assert app["apagar"].is_set()

    await _con_servidor(prueba)
