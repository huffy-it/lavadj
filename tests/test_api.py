"""La API local que consume el panel de escritorio.

Los handlers se llaman directo con una request de mentira: levantar el servidor
de verdad obligaría a ocupar un puerto y no probaría nada más.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from core import ajustes, api, config, tts, voces
from core.dj import DJ
from core.feeds import FeedCache
from core.guionista import Guionista
from core.resolver import Resolver
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
    bot = FakeBot(FakeCog())
    bot.resolver = SimpleNamespace(
        estado_ytdlp=lambda: {"version": "2026.07.04", "dias": 55, "viejo": True}
    )

    datos = cuerpo(await api.handle_status(pedido("GET", "/api/status", bot)))

    assert datos["ytdlp"] == {"version": "2026.07.04", "dias": 55, "viejo": True}


async def test_el_estado_trae_el_cliente_de_youtube_que_usa_el_resolver_de_verdad():
    """Los paneles avisan del cambio comparando `cliente` con `cliente_configurado`."""
    bot = FakeBot(FakeCog())
    bot.resolver = Resolver(cliente="android")
    bot.resolver.cliente = "android_vr"      # lo que hace el sondeo al rotar

    datos = cuerpo(await api.handle_status(pedido("GET", "/api/status", bot)))

    assert datos["ytdlp"]["cliente"] == "android_vr"
    assert datos["ytdlp"]["cliente_configurado"] == "android"


async def test_sin_resolver_en_el_bot_el_estado_de_yt_dlp_conserva_su_forma():
    """El panel web lee `ytdlp.version` sin comprobar nada: no puede faltar."""
    datos = cuerpo(await api.handle_status(pedido("GET", "/api/status")))
    assert datos["ytdlp"] == {
        "version": None, "dias": None, "viejo": False,
        "cliente": None, "cliente_configurado": None,
    }
    assert set(datos["ytdlp"]) == set(Resolver().estado_ytdlp())


async def test_sin_cog_cargado_el_estado_no_revienta():
    """Los dos primeros segundos del arranque el cog todavía no está."""
    datos = cuerpo(await api.handle_status(pedido("GET", "/api/status", FakeBot(None))))
    assert datos["salud"] is None


# --- voces del DJ -----------------------------------------------------------

ALD = "es_MX-ald-medium"


@pytest.fixture
def voz_instalada(monkeypatch):
    """Piper disponible y ALD instalada."""
    monkeypatch.setattr(voces, "piper_disponible", lambda: True)
    config.CARPETA_DE_VOCES.mkdir(parents=True)
    voces.ruta_modelo(ALD).write_bytes(b"onnx")
    voces.ruta_config(ALD).write_bytes(b"{}")


@pytest.fixture
def descargas(monkeypatch):
    """Un gestor de descargas de mentira que solo anota qué se le pidió."""
    class Falso:
        pedidas = []
        fallo = None      # el mensaje con el que `iniciar` se niega

        def iniciar(self, voz_id):
            if self.fallo:
                raise voces.ErrorDeVoz(self.fallo)
            self.pedidas.append(voz_id)

        def activa(self, voz_id):
            return voz_id in self.pedidas

        def progreso(self, voz_id):
            return 40 if voz_id in self.pedidas else None

        def error(self, voz_id):
            return None

    falso = Falso()
    falso.pedidas = []
    monkeypatch.setattr(voces, "descargas", falso)
    return falso


_HANDLERS = {
    "/api/voz": "handle_voz", "/api/voz/filtro": "handle_voz_filtro",
    "/api/voces/descargar": "handle_voces_descargar", "/api/voces/borrar": "handle_voces_borrar",
}


def post(ruta):
    return asyncio.run(getattr(api, _HANDLERS[ruta.split("?")[0]])(pedido("POST", ruta)))


def test_get_voces_trae_el_catalogo_la_voz_en_uso_y_el_filtro(voz_instalada):
    datos = cuerpo(asyncio.run(api.handle_voces(pedido("GET", "/api/voces"))))

    assert [v["id"] for v in datos["voces"]] == list(voces.CATALOGO)
    assert datos["en_uso"] == "espeak" and datos["filtro_radio"] is True and datos["piper"] is True
    ald = next(v for v in datos["voces"] if v["id"] == ALD)
    assert ald["instalada"] and not ald["descargando"] and ald["pais"] == "México"


def test_get_voces_avisa_si_falta_piper(monkeypatch):
    monkeypatch.setattr(voces, "piper_disponible", lambda: False)
    assert cuerpo(asyncio.run(api.handle_voces(pedido("GET", "/api/voces"))))["piper"] is False


def test_elegir_una_voz_instalada(voz_instalada):
    respuesta = post(f"/api/voz?voz={ALD}")

    assert respuesta.status == 200
    assert cuerpo(respuesta)["en_uso"] == ALD
    assert ajustes.obtener("voz") == ALD


def test_elegir_una_voz_sin_instalar_es_409_y_no_cambia_nada():
    respuesta = post(f"/api/voz?voz={ALD}")

    assert respuesta.status == 409 and "instalada" in cuerpo(respuesta)["error"]
    assert ajustes.obtener("voz") == "espeak"


@pytest.mark.parametrize("ruta", [
    "/api/voz?voz=es_XX-nada-low", "/api/voz", "/api/voz?voz=../../etc",
    "/api/voces/descargar?voz=http://evil.example/x", "/api/voces/borrar?voz=es_XX-nada-low",
])
def test_una_voz_que_no_esta_en_el_catalogo_es_404(ruta, descargas):
    respuesta = post(ruta)

    assert respuesta.status == 404
    assert descargas.pedidas == []


def test_descargar_arranca_la_descarga_y_devuelve_el_estado(descargas):
    respuesta = post(f"/api/voces/descargar?voz={ALD}")

    assert respuesta.status == 200
    assert descargas.pedidas == [ALD]
    ald = next(v for v in cuerpo(respuesta)["voces"] if v["id"] == ALD)
    assert ald["descargando"] and ald["progreso"] == 40


def test_descargar_lo_que_no_se_puede_es_409(descargas):
    descargas.fallo = "ya se está descargando"

    respuesta = post(f"/api/voces/descargar?voz={ALD}")

    assert respuesta.status == 409 and cuerpo(respuesta)["error"] == "ya se está descargando"


def test_borrar_una_voz_instalada(voz_instalada):
    respuesta = post(f"/api/voces/borrar?voz={ALD}")

    assert respuesta.status == 200
    assert not voces.instalada(ALD)


def test_no_se_borra_la_voz_en_uso_ni_la_de_siempre(voz_instalada):
    ajustes.guardar(voz=ALD)

    assert post(f"/api/voces/borrar?voz={ALD}").status == 409
    assert post("/api/voces/borrar?voz=espeak").status == 409
    assert voces.instalada(ALD)


def test_el_filtro_de_radio_se_apaga_y_se_enciende():
    apagado = post("/api/voz/filtro?activo=0")
    assert cuerpo(apagado)["filtro_radio"] is False and ajustes.obtener("filtro_radio") is False

    assert cuerpo(post("/api/voz/filtro?activo=1"))["filtro_radio"] is True


@pytest.mark.parametrize("ruta", ["/api/voz/filtro", "/api/voz/filtro?activo=quizá", "/api/voz/filtro?activo=2"])
def test_el_filtro_con_basura_es_400(ruta):
    assert post(ruta).status == 400
    assert ajustes.obtener("filtro_radio") is True


@pytest.fixture
def sintesis(monkeypatch, tmp_path):
    """tts.synthesize de mentira: deja un wav y anota con qué voz se lo pidieron."""
    pedidos = []

    async def falsa(texto, voice=tts.VOICE, volume=100, voz=None):
        pedidos.append((texto, voz))
        if voz == "falla":
            return None
        ruta = tmp_path / "dj_muestra.wav"
        ruta.write_bytes(b"RIFF....WAVE")
        return ruta

    monkeypatch.setattr(tts, "synthesize", falsa)
    return pedidos


def muestra(voz):
    return asyncio.run(api.handle_voces_muestra(pedido("GET", f"/api/voces/muestra?voz={voz}")))


def test_la_muestra_devuelve_un_wav_y_borra_el_archivo(voz_instalada, sintesis, tmp_path):
    respuesta = muestra(ALD)

    assert respuesta.status == 200 and respuesta.content_type == "audio/wav"
    assert respuesta.body == b"RIFF....WAVE"
    assert sintesis == [(api.MUESTRA, ALD)]
    assert not (tmp_path / "dj_muestra.wav").exists()


def test_la_muestra_de_espeak_no_pide_instalar_nada(sintesis):
    assert muestra("espeak").status == 200
    assert sintesis == [(api.MUESTRA, "espeak")]


def test_la_muestra_de_una_voz_sin_instalar_es_409(sintesis):
    assert muestra(ALD).status == 409
    assert sintesis == []


def test_la_muestra_de_una_voz_desconocida_es_404(sintesis):
    assert muestra("es_XX-nada-low").status == 404
    assert sintesis == []


def test_si_no_se_puede_sintetizar_la_muestra_es_503(monkeypatch, sintesis):
    monkeypatch.setitem(voces.CATALOGO, "falla", voces.Voz("falla"))
    assert muestra("falla").status == 503


# --- guionista del DJ -------------------------------------------------------

@pytest.fixture
def guionista(monkeypatch):
    """Un Guionista de verdad (sin elección en ajustes) cuyo `disponibles` no sale a la red: se mueve con `.estan`."""
    monkeypatch.setattr(config, "DJ_GUIONISTA", "frases")
    monkeypatch.setattr(config, "DJ_GUIONISTA_MODELO", "claude-opus-5-5")
    monkeypatch.setattr(config, "OLLAMA_MODELO", "llama3.2:3b")
    g = Guionista()
    g.estan, g.sesiones = {"frases": True, "claude": True, "ollama": True}, []

    async def disponibles(session=None):
        g.sesiones.append(session)
        return dict(g.estan)

    monkeypatch.setattr(g, "disponibles", disponibles)
    return g


def con_guionista(metodo, ruta, guionista, bot=None):
    bot = bot or FakeBot(FakeCog(dj=DJ(guionista=guionista), session="la sesión"))
    return asyncio.run(api.handle_guionista(pedido(metodo, ruta, bot)))


def test_get_guionista_trae_el_activo_los_disponibles_el_error_y_los_modelos(guionista):
    guionista.elegir("claude")
    guionista.ultimo_error = "claude: la API respondió 500"
    guionista.estan["ollama"] = False

    respuesta = con_guionista("GET", "/api/guionista", guionista)

    assert respuesta.status == 200
    assert cuerpo(respuesta) == {
        "activo": "claude",
        "disponibles": {"frases": True, "claude": True, "ollama": False},
        "ultimo_error": "claude: la API respondió 500",
        "modelos": {"claude": "claude-opus-5-5", "ollama": "llama3.2:3b"},
    }
    assert guionista.sesiones == ["la sesión"]


def test_get_guionista_sin_errores_ni_eleccion_guardada(guionista):
    datos = cuerpo(con_guionista("GET", "/api/guionista", guionista))

    assert datos["activo"] == "frases" and datos["ultimo_error"] is None
    assert ajustes.obtener("guionista") is None


@pytest.mark.parametrize("nombre", ["claude", "ollama"])
def test_post_guionista_cambia_en_caliente_y_guarda_la_eleccion(guionista, nombre):
    respuesta = con_guionista("POST", f"/api/guionista?nombre={nombre}", guionista)

    assert respuesta.status == 200
    assert cuerpo(respuesta)["activo"] == nombre
    assert guionista.activo == nombre
    assert ajustes.obtener("guionista") == nombre


def test_la_eleccion_guardada_por_la_api_es_la_que_arranca_la_proxima_vez(guionista):
    con_guionista("POST", "/api/guionista?nombre=ollama", guionista)

    assert Guionista().activo == "ollama"


@pytest.mark.parametrize("ruta", [
    "/api/guionista?nombre=gpt", "/api/guionista", "/api/guionista?nombre=", "/api/guionista?nombre=Claude",
    "/api/guionista?nombre=../../etc",
])
def test_post_guionista_con_un_nombre_que_no_existe_es_400(guionista, ruta):
    guionista.elegir("ollama")

    respuesta = con_guionista("POST", ruta, guionista)

    assert respuesta.status == 400 and "desconocido" in cuerpo(respuesta)["error"]
    assert guionista.activo == "ollama"
    assert ajustes.obtener("guionista") is None


@pytest.mark.parametrize("nombre", ["claude", "ollama"])
def test_post_de_un_motor_que_no_esta_disponible_es_409_y_no_cambia_nada(guionista, nombre):
    guionista.estan[nombre] = False

    respuesta = con_guionista("POST", f"/api/guionista?nombre={nombre}", guionista)

    assert respuesta.status == 409 and nombre in cuerpo(respuesta)["error"]
    assert guionista.activo == "frases"
    assert ajustes.obtener("guionista") is None


def test_las_frases_siempre_se_pueden_elegir_aunque_nada_este_disponible(guionista):
    guionista.elegir("claude")
    guionista.estan = {"frases": False, "claude": False, "ollama": False}

    respuesta = con_guionista("POST", "/api/guionista?nombre=frases", guionista)

    assert respuesta.status == 200 and cuerpo(respuesta)["activo"] == "frases"
    assert guionista.activo == "frases"
    assert ajustes.obtener("guionista") == "frases"


def test_si_no_se_puede_guardar_la_eleccion_es_500_y_no_cambia_nada(guionista, monkeypatch):
    def falla(**_):
        raise OSError("disco lleno")

    monkeypatch.setattr(ajustes, "guardar", falla)

    respuesta = con_guionista("POST", "/api/guionista?nombre=claude", guionista)

    assert respuesta.status == 500 and "disco lleno" in cuerpo(respuesta)["error"]
    assert guionista.activo == "frases"


@pytest.mark.parametrize("metodo, ruta", [("GET", "/api/guionista"), ("POST", "/api/guionista?nombre=claude")])
def test_guionista_sin_cog_es_503(guionista, metodo, ruta):
    respuesta = con_guionista(metodo, ruta, guionista, bot=FakeBot(None))

    assert respuesta.status == 503
    assert ajustes.obtener("guionista") is None


async def test_las_rutas_del_guionista_existen_y_pasan_por_la_proteccion(guionista, monkeypatch):
    async def disponibles(self, session=None):
        return dict(guionista.estan)

    monkeypatch.setattr(Guionista, "disponibles", disponibles)

    async def prueba(cliente, puerto, app):
        r = await cliente.get("/api/guionista")
        assert r.status == 200 and (await r.json())["activo"] == "frases"
        assert (await cliente.get("/api/guionista", headers={"Host": "evil.example.com"})).status == 403

        r = await cliente.post("/api/guionista?nombre=claude", headers={"Origin": "http://evil.example.com"})
        assert r.status == 403 and ajustes.obtener("guionista") is None

        r = await cliente.post("/api/guionista?nombre=claude", headers={"Origin": f"http://127.0.0.1:{puerto}"})
        assert r.status == 200 and ajustes.obtener("guionista") == "claude"

    await _con_servidor(prueba)


# --- panel web --------------------------------------------------------------

def test_la_pagina_del_panel_trae_la_seccion_de_voces():
    pagina = asyncio.run(api.handle_pagina(pedido("GET", "/"))).text
    for pieza in ("api('voces')", "voces/descargar", "voces/borrar", "voz/filtro", "voces/muestra", "esc("):
        assert pieza in pagina


def test_la_pagina_del_panel_trae_la_seccion_del_guionista():
    pagina = asyncio.run(api.handle_pagina(pedido("GET", "/"))).text
    for pieza in ("api('guionista", "Guionista del DJ", "Frases fijas", "Claude (API, cobra por uso)", "Ollama (local)", "esc("):
        assert pieza in pagina


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


async def test_las_rutas_de_voces_existen_y_pasan_por_la_proteccion(sintesis):
    async def prueba(cliente, puerto, app):
        r = await cliente.get("/api/voces")
        assert r.status == 200 and "voces" in await r.json()

        r = await cliente.get("/api/voces/muestra?voz=espeak")
        assert r.status == 200 and r.content_type == "audio/wav"

        for ruta in ("/api/voces", "/api/voces/muestra?voz=espeak"):
            assert (await cliente.get(ruta, headers={"Host": "evil.example.com"})).status == 403
        for ruta in ("/api/voz?voz=espeak", "/api/voz/filtro?activo=0",
                     f"/api/voces/descargar?voz={ALD}", f"/api/voces/borrar?voz={ALD}"):
            assert (await cliente.post(ruta, headers={"Origin": "http://evil.example.com"})).status == 403
        assert ajustes.obtener("filtro_radio") is True

        r = await cliente.post("/api/voz/filtro?activo=0", headers={"Origin": f"http://127.0.0.1:{puerto}"})
        assert r.status == 200 and ajustes.obtener("filtro_radio") is False

    await _con_servidor(prueba)
