"""La API local que consume el panel. Los handlers se llaman directo con una request de mentira."""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request, unused_port

from cogs.music import Music
from core import ajustes, api, config, lyrics, resolver, tts, voces
from core.dj import DJ
from core.feeds import FeedCache
from core.guionista import Guionista
from core.lyrics import Letra
from core.resolver import Resolver
from core.salud import Salud

SNOWFLAKE = 1234567890123456789      # no cabe en un número de JavaScript: tiene que viajar como texto
ALD = "es_MX-ald-medium"
EVIL_HOST = {"Host": "evil.example.com"}
EVIL_ORIGIN = {"Origin": "http://evil.example.com"}

class FakeCog:
    def __init__(self, dj=None, session="sesión", salud=None):
        self.dj = dj if dj is not None else DJ()
        self._session = session
        self.salud = salud if salud is not None else Salud()

class CogConPosicion(FakeCog):
    @staticmethod
    def posicion_viva(vc):
        return 4_000

class FakeBot:
    def __init__(self, cog=None):
        self.cog = cog
        self.guilds = []
        self.user = "LavaDJ#7584"
        self.apagar_todo = False

    def get_cog(self, nombre):
        return self.cog

    def is_ready(self):
        return True

def pedido(metodo, ruta, bot=None, letras=None):
    app = web.Application()
    app["bot"] = bot if bot is not None else FakeBot(FakeCog())
    app["apagar"] = asyncio.Event()
    app["arranque"] = 0.0
    app["letras"] = letras if letras is not None else lyrics.Letras()
    return make_mocked_request(metodo, ruta, app=app)

async def llamar(handler, metodo, ruta, bot=None, letras=None):
    return await getattr(api, handler)(pedido(metodo, ruta, bot, letras))

def cuerpo(respuesta: web.Response) -> dict:
    return json.loads(respuesta.body)

def pista(titulo="Never Gonna Give You Up", autor="Rick Astley - Topic", id="dQw4w9WgXcQ", largo=213_000, **extra):
    atributos = dict(
        title=titulo, author=autor, identifier=id, uri=f"https://www.youtube.com/watch?v={id}", length=largo, extras={},
    )
    return SimpleNamespace(**{**atributos, **extra})

def sala(actual=None, posicion=0, pausado=False, id=SNOWFLAKE, nombre="Servidor"):
    """Un servidor con su voice_client de mentira; sin `actual` no suena nada."""
    vc = SimpleNamespace(
        current=actual, playing=actual is not None, paused=pausado, position=posicion, queue=[1, 2],
        channel=SimpleNamespace(name="General"),
    )
    guild = SimpleNamespace(id=id, name=nombre, voice_client=vc)
    vc.guild = guild
    return guild

def bot_con(*salas, cog=None):
    bot = FakeBot(cog)
    bot.guilds = list(salas)
    return bot

async def _con_servidor(prueba):
    """Levanta la app de verdad en un puerto libre (el middleware compara contra ese puerto)."""
    puerto = unused_port()
    app = api.crear_app(FakeBot(FakeCog()), asyncio.Event(), puerto)
    async with TestClient(TestServer(app, port=puerto)) as cliente:
        await prueba(cliente, puerto, app)

async def _bloqueadas(cliente, gets=(), posts=()):
    """Con Host u Origin ajeno, las rutas dan 403."""
    for ruta in gets:
        assert (await cliente.get(ruta, headers=EVIL_HOST)).status == 403, ruta
    for ruta in posts:
        assert (await cliente.post(ruta, headers=EVIL_ORIGIN)).status == 403, ruta

# --- volumen del DJ ---------------------------------------------------------

@pytest.mark.parametrize("metodo, query, status, volumen", [
    ("GET", "", 200, 70), ("POST", "?valor=45", 200, 45), ("POST", "?valor=900", 200, 200),   # topea en 200
    ("POST", "?valor=fuerte", 400, 70),      # con basura no se mueve
])
async def test_dj_volume(metodo, query, status, volumen):
    cog = FakeCog()
    cog.dj.volume = 70
    respuesta = await llamar("handle_dj_volume", metodo, "/api/dj_volume" + query, FakeBot(cog))
    assert respuesta.status == status and cog.dj.volume == volumen
    if status == 200:
        assert cuerpo(respuesta)["volumen"] == volumen

async def test_dj_volume_sin_cog_es_503():
    assert (await llamar("handle_dj_volume", "GET", "/api/dj_volume", FakeBot(None))).status == 503

# --- estado de voz ----------------------------------------------------------

async def test_el_estado_de_voz_trae_la_posicion_el_id_y_la_pista():
    bot = bot_con(sala(pista(), posicion=65_000), cog=FakeCog())
    (voz,) = api._estado_voz(bot)
    assert voz == {
        "id": str(SNOWFLAKE), "servidor": "Servidor", "canal": "General", "sonando": True,
        "cancion": "Never Gonna Give You Up", "en_cola": 2,
        "posicion_ms": 65_000, "pausado": False, "duracion_ms": 213_000, "pista": "dQw4w9WgXcQ", "dj": False,
    }
    assert json.dumps(voz)
    assert cuerpo(await llamar("handle_status", "GET", "/api/status", bot))["voz"] == [voz]

# La URL resuelta cambia en cada resolución; el origen guardado en `extras` no.
RESUELTA = pista(id="https://rr1.googlevideo.com/videoplayback?x=1", extras={
    resolver.CLAVE_ORIGEN: {"identifier": "dQw4w9WgXcQ", "uri": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
})

@pytest.mark.parametrize("guild, cog, esperado", [
    (sala(pista(), posicion=30_000, pausado=True), FakeCog(), {"pausado": True, "posicion_ms": 30_000}),
    (sala(), FakeCog(), {"sonando": False, "cancion": None, "pista": None, "posicion_ms": 0, "duracion_ms": 0, "dj": False}),
    (sala(pista(largo=200_000), posicion=999_000), FakeCog(), {"posicion_ms": 200_000}),
    (sala(RESUELTA), FakeCog(), {"pista": "dQw4w9WgXcQ"}),
    (sala(pista(id="C:/dj_cache/clip.wav", is_dj_clip=True)), FakeCog(), {"dj": True, "pista": None}),
    # wavelink arrastra la posición del tema anterior hasta el primer reporte de Lavalink; el cog la corrige.
    (sala(pista(), posicion=180_000), CogConPosicion(), {"posicion_ms": 4_000}),
    (sala(pista(), posicion=7_000), None, {"posicion_ms": 7_000}),
], ids=["pausa", "nada sonando", "tope de la duración", "id de YouTube", "corte del DJ", "posición del cog", "sin cog"])
def test_el_estado_de_voz_segun_el_caso(guild, cog, esperado):
    (voz,) = api._estado_voz(bot_con(guild, cog=cog))
    assert {k: voz[k] for k in esperado} == esperado

# --- letra ------------------------------------------------------------------

@pytest.fixture
def busqueda(monkeypatch):
    """`lyrics.find_synced` de mentira: anota cada llamada y contesta `.letra` (o lanza `.error`)."""
    b = SimpleNamespace(letra=Letra(True, [(1000, "uno"), (5000, "dos & <b>tres</b>")]), error=None, llamadas=[], durante=None)

    async def falsa(session, titulo, artista, cancion, duracion_ms=0):
        b.llamadas.append((session, titulo, artista, cancion, duracion_ms))
        if b.durante:
            b.durante()
        if b.error:
            raise b.error
        return b.letra

    monkeypatch.setattr(lyrics, "find_synced", falsa)
    return b

async def letra(bot, ruta="/api/letra", letras=None):
    return await llamar("handle_letra", "GET", ruta, bot, letras)

@pytest.mark.parametrize("posicion, pausado", [(65_000, False), (42_000, True)])
async def test_letra_sincronizada_trae_las_lineas_con_tiempo_y_la_posicion(busqueda, posicion, pausado):
    respuesta = await letra(bot_con(sala(pista(), posicion=posicion, pausado=pausado), cog=FakeCog()))
    assert respuesta.status == 200
    datos = cuerpo(respuesta)
    assert isinstance(datos.pop("ahora_ms"), int)
    assert datos == {
        "servidor": str(SNOWFLAKE), "cancion": "Never Gonna Give You Up", "pista": "dQw4w9WgXcQ", "motivo": None,
        "sincronizada": True, "lineas": [{"ms": 1000, "texto": "uno"}, {"ms": 5000, "texto": "dos & <b>tres</b>"}],
        "posicion_ms": posicion, "pausado": pausado, "duracion_ms": 213_000, "dj": False,
    }

@pytest.mark.parametrize("titulo, canal, artista, cancion", [
    ("Never Gonna Give You Up", "Rick Astley - Topic", "Rick Astley", "Never Gonna Give You Up"),
    ("Soda Stereo - De Música Ligera (Official Video)", "SodaStereoVEVO", "Soda Stereo", "De Música Ligera"),
])
async def test_letra_busca_con_la_sesion_del_cog_el_titulo_el_artista_y_la_duracion(busqueda, titulo, canal, artista, cancion):
    await letra(bot_con(sala(pista(titulo, canal)), cog=FakeCog(session="la sesión")))
    assert busqueda.llamadas == [("la sesión", titulo, artista, cancion, 213_000)]

async def test_letra_sin_sesion_del_cog_usa_una_propia(busqueda):
    await letra(bot_con(sala(pista()), cog=FakeCog(session=None)))
    await letra(bot_con(sala(pista(id="otra")), cog=None))
    assert len(busqueda.llamadas) == 2
    assert all(isinstance(llamada[0], aiohttp.ClientSession) for llamada in busqueda.llamadas)

async def test_letra_sin_sincronizar_trae_solo_texto(busqueda):
    busqueda.letra = Letra(False, [(None, "uno"), (None, ""), (None, "dos")])
    datos = cuerpo(await letra(bot_con(sala(pista()), cog=FakeCog())))
    assert datos["sincronizada"] is False and datos["motivo"] is None
    assert datos["lineas"] == [{"texto": "uno"}, {"texto": ""}, {"texto": "dos"}]

@pytest.mark.parametrize("error, motivo", [(None, "sin_letra"), (RuntimeError("bug"), "error")])
async def test_letra_que_no_existe_o_revienta_sale_vacia_con_motivo_y_sin_ser_un_error(busqueda, error, motivo):
    busqueda.letra, busqueda.error = None, error
    respuesta = await letra(bot_con(sala(pista(), posicion=9_000), cog=FakeCog()))
    datos = cuerpo(respuesta)
    assert respuesta.status == 200
    assert datos["motivo"] == motivo and datos["lineas"] == [] and datos["sincronizada"] is False
    assert datos["pista"] == "dQw4w9WgXcQ"
    if error is None:
        assert (datos["cancion"], datos["posicion_ms"]) == ("Never Gonna Give You Up", 9_000)

@pytest.mark.parametrize("salas", [[], [sala()]], ids=["sin servidores", "servidor en silencio"])
async def test_letra_sin_nada_sonando_sale_vacia_con_motivo(busqueda, salas):
    respuesta = await letra(bot_con(*salas, cog=FakeCog()))
    datos = cuerpo(respuesta)
    assert respuesta.status == 200 and datos["motivo"] == "nada_sonando" and datos["lineas"] == []
    assert datos["cancion"] is None and datos["servidor"] is None and datos["pista"] is None
    assert busqueda.llamadas == []

async def test_letra_con_el_dj_hablando_no_busca_nada(busqueda):
    datos = cuerpo(await letra(bot_con(sala(pista(id="C:/dj_cache/clip.wav", is_dj_clip=True)), cog=FakeCog())))
    assert datos["motivo"] == "dj" and datos["dj"] is True and datos["lineas"] == []
    assert busqueda.llamadas == []

async def test_letra_es_la_del_servidor_pedido_o_la_del_primero_que_suena(busqueda):
    bot = bot_con(sala(), sala(pista("Primera", id="a"), id=2), sala(pista("Segunda", id="b"), id=SNOWFLAKE), cog=FakeCog())
    sin = cuerpo(await letra(bot))
    con = cuerpo(await letra(bot, f"/api/letra?servidor={SNOWFLAKE}"))
    assert (sin["servidor"], sin["cancion"]) == ("2", "Primera")
    assert (con["servidor"], con["cancion"]) == (str(SNOWFLAKE), "Segunda")
    assert [llamada[1] for llamada in busqueda.llamadas] == ["Primera", "Segunda"]

@pytest.mark.parametrize("servidor", ["999", "abc", "../../etc", "3"])
async def test_letra_de_un_servidor_que_no_suena_o_no_existe_sale_vacia(busqueda, servidor):
    bot = bot_con(sala(pista(), id=2), sala(id=3), cog=FakeCog())
    respuesta = await letra(bot, f"/api/letra?servidor={servidor}")
    assert respuesta.status == 200 and cuerpo(respuesta)["motivo"] == "nada_sonando"
    assert busqueda.llamadas == []

@pytest.mark.parametrize("hay_letra", [True, False])
async def test_letra_se_busca_una_sola_vez_por_pista_pero_otra_pista_se_busca_de_nuevo(busqueda, hay_letra):
    if not hay_letra:
        busqueda.letra = None
    letras, guild = lyrics.Letras(), sala(pista(id="a"), posicion=1_000)
    bot = bot_con(guild, cog=FakeCog())
    primera = cuerpo(await letra(bot, letras=letras))
    guild.voice_client.position = 20_000
    segunda = cuerpo(await letra(bot, letras=letras))
    assert len(busqueda.llamadas) == 1      # también si no se encontró
    assert primera["lineas"] == segunda["lineas"]
    assert (primera["posicion_ms"], segunda["posicion_ms"]) == (1_000, 20_000)      # la posición sí se mide cada vez
    guild.voice_client.current = pista(id="b")
    await letra(bot, letras=letras)
    assert len(busqueda.llamadas) == 2

async def test_letra_si_la_pista_cambia_mientras_se_busca_no_vale(busqueda):
    guild = sala(pista(id="a"), posicion=500)
    busqueda.durante = lambda: setattr(guild.voice_client, "current", pista(id="b"))
    datos = cuerpo(await letra(bot_con(guild, cog=FakeCog())))
    assert datos["motivo"] == "cambio" and datos["lineas"] == []

@pytest.mark.parametrize("tipo", ["con_letras", "sin_letras", "sin_cog"])
async def test_letra_usa_la_cache_del_cog_si_la_tiene_y_si_no_la_de_la_app(busqueda, tipo):
    del_cog, de_la_app = lyrics.Letras(), lyrics.Letras()
    cog = None if tipo == "sin_cog" else FakeCog()
    if tipo == "con_letras":
        cog.letras = del_cog
    usada, otra = (del_cog, de_la_app) if tipo == "con_letras" else (de_la_app, del_cog)
    bot = bot_con(sala(pista(), posicion=1_000), cog=cog)
    await letra(bot, letras=de_la_app)
    await letra(bot, letras=de_la_app)
    assert len(busqueda.llamadas) == 1
    assert list(usada._datos) == ["dQw4w9WgXcQ"] and list(otra._datos) == []

async def test_lo_que_ya_busco_la_letra_en_vivo_de_discord_no_se_busca_otra_vez_para_el_panel_web(busqueda):
    cog, actual = Music(bot=None), pista()
    await cog._buscar_letra(actual)()
    datos = cuerpo(await letra(bot_con(sala(actual, posicion=3_000), cog=cog)))
    assert len(busqueda.llamadas) == 1
    assert datos["motivo"] is None and datos["lineas"][0] == {"ms": 1000, "texto": "uno"}

async def test_la_ruta_de_la_letra_existe_y_pasa_por_la_proteccion():
    async def prueba(cliente, puerto, app):
        r = await cliente.get("/api/letra")
        assert r.status == 200 and (await r.json())["motivo"] == "nada_sonando"
        await _bloqueadas(cliente, gets=["/api/letra"])

    await _con_servidor(prueba)

# --- radio de lo que suena --------------------------------------------------

class CogConRadio(FakeCog):
    """`poner_radio` anota a qué servidor se lo pidieron y contesta `aviso`, o lanza `error`."""

    def __init__(self, aviso="Radio armada desde lo que suena.", error=None):
        super().__init__()
        self.aviso, self.error, self.pedidos = aviso, error, []

    async def poner_radio(self, guild):
        self.pedidos.append(guild.id)
        if self.error:
            raise self.error
        return self.aviso

async def radio(bot, ruta="/api/radio"):
    return await llamar("handle_radio", "POST", ruta, bot)

async def test_radio_se_arma_en_el_servidor_pedido_o_en_el_primero_que_suena():
    cog = CogConRadio()
    bot = bot_con(sala(), sala(pista(), id=2), sala(pista(id="b"), id=SNOWFLAKE), cog=cog)
    sin, con = await radio(bot), await radio(bot, f"/api/radio?servidor={SNOWFLAKE}")
    assert (sin.status, cuerpo(sin)) == (200, {"ok": True, "aviso": "Radio armada desde lo que suena."})
    assert con.status == 200 and cog.pedidos == [2, SNOWFLAKE]

@pytest.mark.parametrize("salas, query", [
    ([], ""), ([sala()], ""), ([sala(pista(), id=2), sala(id=3)], "?servidor=3"), ([sala(pista(), id=2)], "?servidor=999"),
], ids=["sin servidores", "servidor en silencio", "el pedido no suena", "el pedido no existe"])
async def test_radio_sin_nada_sonando_da_409_y_no_toca_el_cog(salas, query):
    cog = CogConRadio()
    respuesta = await radio(bot_con(*salas, cog=cog), "/api/radio" + query)
    assert respuesta.status == 409 and cuerpo(respuesta)["ok"] is False and cuerpo(respuesta)["aviso"]
    assert cog.pedidos == []

async def test_radio_sin_el_cog_da_503():
    respuesta = await radio(bot_con(sala(pista()), cog=None))
    assert respuesta.status == 503 and "cog" in cuerpo(respuesta)["error"]

async def test_radio_que_revienta_da_500_con_un_aviso_legible():
    respuesta = await radio(bot_con(sala(pista()), cog=CogConRadio(error=RuntimeError("bug"))))
    datos = cuerpo(respuesta)
    assert respuesta.status == 500 and datos["ok"] is False and "bug" not in datos["aviso"]

async def test_la_ruta_de_la_radio_existe_y_pasa_por_la_proteccion():
    async def prueba(cliente, puerto, app):
        assert (await cliente.post("/api/radio")).status == 409       # llega al handler: no suena nada
        assert (await cliente.get("/api/radio")).status == 405        # cambia el estado: solo POST
        await _bloqueadas(cliente, posts=["/api/radio"])

    await _con_servidor(prueba)

# --- titulares y refrescar --------------------------------------------------

async def test_headlines_devuelve_la_foto_de_la_cache():
    cog = FakeCog()
    cog.dj.feeds = FeedCache(["https://feed/1"])
    cog.dj.feeds._headlines = ["Alcalde inaugura un puente", "Sube el pan"]
    cog.dj.feeds._recent.append("Sube el pan")
    datos = cuerpo(await llamar("handle_headlines", "GET", "/api/headlines", FakeBot(cog)))
    assert [t["texto"] for t in datos["titulares"]] == ["Alcalde inaugura un puente", "Sube el pan"]
    assert [t["leido"] for t in datos["titulares"]] == [False, True]

async def test_headlines_sin_feeds_configurados_no_es_un_error():
    """Sin dj_feeds.json el DJ no tiene caché: el panel lo dice, no revienta."""
    cog = FakeCog()
    cog.dj.feeds = None
    respuesta = await llamar("handle_headlines", "GET", "/api/headlines", FakeBot(cog))
    assert respuesta.status == 200
    assert (cuerpo(respuesta)["titulares"], cuerpo(respuesta)["fuentes"]) == ([], 0)

async def test_refresh_baja_de_nuevo_y_devuelve_los_titulares(monkeypatch):
    cog = FakeCog()
    cog.dj.feeds = FeedCache(["https://feed/1"])
    bajadas = []

    async def falso_refresh_all(session):
        bajadas.append(session)
        cog.dj.feeds._headlines = ["Titular recién bajado"]
        return 1

    monkeypatch.setattr(api, "refresh_feeds", falso_refresh_all)
    datos = cuerpo(await llamar("handle_refresh", "POST", "/api/refresh_news", FakeBot(cog)))
    assert bajadas == ["sesión"] and datos["cleared"] == 1
    assert [t["texto"] for t in datos["titulares"]] == ["Titular recién bajado"]

async def test_refresh_sin_sesion_igual_vence(monkeypatch):
    vencidas = []
    monkeypatch.setattr(api, "invalidate_feeds", lambda: vencidas.append(True) or 1)
    respuesta = await llamar("handle_refresh", "POST", "/api/refresh_news", FakeBot(FakeCog(session=None)))
    assert respuesta.status == 200 and vencidas == [True]

# --- estado -----------------------------------------------------------------

async def estado(bot=None):
    return cuerpo(await llamar("handle_status", "GET", "/api/status", bot))

async def test_el_estado_dice_como_viene_la_transmision():
    """El panel no puede notar un bot mudo si la API solo dice "running"."""
    salud = (await estado())["salud"]
    assert (salud["estado"], salud["fallos_seguidos"], salud["hueco"]) == ("bien", 0, None)
    marca = {"t": 0.0}
    con_hueco = Salud(reloj=lambda: marca["t"])
    con_hueco.termino(1)
    marca["t"] = 2.5
    con_hueco.arranco(1)
    hueco = (await estado(FakeBot(FakeCog(salud=con_hueco))))["salud"]["hueco"]
    assert hueco == {"ultimo": 2.5, "promedio": 2.5, "maximo": 2.5, "muestras": 1}
    fallando = Salud()
    fallando.fallo()
    fallando.fallo()
    salud = (await estado(FakeBot(FakeCog(salud=fallando))))["salud"]
    assert salud["estado"] == "flojo" and salud["motivo"]

async def test_el_estado_trae_el_ytdlp_del_resolver_de_verdad_con_su_cliente():
    """Los paneles avisan del cambio comparando `cliente` con `cliente_configurado`."""
    bot = FakeBot(FakeCog())
    bot.resolver = Resolver(cliente="android")
    bot.resolver.cliente = "android_vr"      # lo que hace el sondeo al rotar
    datos = (await estado(bot))["ytdlp"]
    assert datos == bot.resolver.estado_ytdlp()
    assert datos["cliente"] == "android_vr" and datos["cliente_configurado"] == "android"
    bot.resolver = SimpleNamespace(estado_ytdlp=lambda: {"version": "2026.07.04", "dias": 55, "viejo": True})
    assert (await estado(bot))["ytdlp"] == {"version": "2026.07.04", "dias": 55, "viejo": True}

async def test_sin_resolver_ni_cog_el_estado_conserva_su_forma():
    """El panel web lee `ytdlp.version` sin comprobar nada; el cog tarda dos segundos en cargar."""
    datos = await estado(FakeBot(None))
    assert datos["ytdlp"] == {"version": None, "dias": None, "viejo": False, "cliente": None, "cliente_configurado": None}
    assert set(datos["ytdlp"]) == set(Resolver().estado_ytdlp())
    assert datos["salud"] is None

# --- voces del DJ -----------------------------------------------------------

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
        def __init__(self):
            self.pedidas, self.fallo = [], None      # fallo: el mensaje con el que `iniciar` se niega

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
    monkeypatch.setattr(voces, "descargas", falso)
    return falso

_HANDLERS = {
    "/api/voz": "handle_voz", "/api/voz/filtro": "handle_voz_filtro",
    "/api/voces/descargar": "handle_voces_descargar", "/api/voces/borrar": "handle_voces_borrar",
}

async def post(ruta):
    return await llamar(_HANDLERS[ruta.split("?")[0]], "POST", ruta)

async def test_get_voces_trae_el_catalogo_la_voz_en_uso_y_el_filtro(voz_instalada, monkeypatch):
    datos = cuerpo(await llamar("handle_voces", "GET", "/api/voces"))
    assert [v["id"] for v in datos["voces"]] == list(voces.CATALOGO)
    assert datos["en_uso"] == "espeak" and datos["filtro_radio"] is True and datos["piper"] is True
    ald = next(v for v in datos["voces"] if v["id"] == ALD)
    assert ald["instalada"] and not ald["descargando"] and ald["pais"] == "México"
    monkeypatch.setattr(voces, "piper_disponible", lambda: False)
    assert cuerpo(await llamar("handle_voces", "GET", "/api/voces"))["piper"] is False

async def test_elegir_una_voz_sin_instalar_es_409_y_no_cambia_nada():
    respuesta = await post(f"/api/voz?voz={ALD}")
    assert respuesta.status == 409 and "instalada" in cuerpo(respuesta)["error"]
    assert ajustes.obtener("voz") == "espeak"

async def test_elegir_una_voz_instalada(voz_instalada):
    respuesta = await post(f"/api/voz?voz={ALD}")
    assert respuesta.status == 200 and cuerpo(respuesta)["en_uso"] == ALD
    assert ajustes.obtener("voz") == ALD

@pytest.mark.parametrize("ruta", [
    "/api/voz?voz=es_XX-nada-low", "/api/voz", "/api/voz?voz=../../etc",
    "/api/voces/descargar?voz=http://evil.example/x", "/api/voces/borrar?voz=es_XX-nada-low",
])
async def test_una_voz_que_no_esta_en_el_catalogo_es_404(ruta, descargas):
    assert (await post(ruta)).status == 404
    assert descargas.pedidas == []

async def test_descargar_arranca_la_descarga_y_devuelve_el_estado(descargas):
    respuesta = await post(f"/api/voces/descargar?voz={ALD}")
    assert respuesta.status == 200 and descargas.pedidas == [ALD]
    ald = next(v for v in cuerpo(respuesta)["voces"] if v["id"] == ALD)
    assert ald["descargando"] and ald["progreso"] == 40

async def test_descargar_lo_que_no_se_puede_es_409(descargas):
    descargas.fallo = "ya se está descargando"
    respuesta = await post(f"/api/voces/descargar?voz={ALD}")
    assert respuesta.status == 409 and cuerpo(respuesta)["error"] == "ya se está descargando"

async def test_borrar_una_voz_instalada_pero_no_la_en_uso_ni_la_de_siempre(voz_instalada):
    ajustes.guardar(voz=ALD)
    assert (await post(f"/api/voces/borrar?voz={ALD}")).status == 409
    assert (await post("/api/voces/borrar?voz=espeak")).status == 409
    assert voces.instalada(ALD)
    ajustes.guardar(voz="espeak")
    assert (await post(f"/api/voces/borrar?voz={ALD}")).status == 200
    assert not voces.instalada(ALD)

async def test_el_filtro_de_radio_se_apaga_y_se_enciende():
    apagado = await post("/api/voz/filtro?activo=0")
    assert cuerpo(apagado)["filtro_radio"] is False and ajustes.obtener("filtro_radio") is False
    assert cuerpo(await post("/api/voz/filtro?activo=1"))["filtro_radio"] is True

@pytest.mark.parametrize("ruta", ["/api/voz/filtro", "/api/voz/filtro?activo=quizá", "/api/voz/filtro?activo=2"])
async def test_el_filtro_con_basura_es_400(ruta):
    assert (await post(ruta)).status == 400
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

async def muestra(voz):
    return await llamar("handle_voces_muestra", "GET", f"/api/voces/muestra?voz={voz}")

async def test_la_muestra_devuelve_un_wav_y_borra_el_archivo(voz_instalada, sintesis, tmp_path):
    respuesta = await muestra(ALD)
    assert respuesta.status == 200 and respuesta.content_type == "audio/wav" and respuesta.body == b"RIFF....WAVE"
    assert sintesis == [(api.MUESTRA, ALD)]
    assert not (tmp_path / "dj_muestra.wav").exists()

async def test_la_muestra_de_espeak_no_pide_instalar_nada(sintesis):
    assert (await muestra("espeak")).status == 200
    assert sintesis == [(api.MUESTRA, "espeak")]

@pytest.mark.parametrize("voz, status", [(ALD, 409), ("es_XX-nada-low", 404)], ids=["sin instalar", "desconocida"])
async def test_la_muestra_de_una_voz_sin_instalar_o_desconocida_no_sintetiza(sintesis, voz, status):
    assert (await muestra(voz)).status == status
    assert sintesis == []

async def test_si_no_se_puede_sintetizar_la_muestra_es_503(monkeypatch, sintesis):
    monkeypatch.setitem(voces.CATALOGO, "falla", voces.Voz("falla"))
    assert (await muestra("falla")).status == 503

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

async def con_guionista(metodo, ruta, guionista, bot=None):
    bot = bot or FakeBot(FakeCog(dj=DJ(guionista=guionista), session="la sesión"))
    return await llamar("handle_guionista", metodo, ruta, bot)

async def test_get_guionista_trae_el_activo_los_disponibles_el_error_y_los_modelos(guionista):
    datos = cuerpo(await con_guionista("GET", "/api/guionista", guionista))
    assert datos["activo"] == "frases" and datos["ultimo_error"] is None
    assert ajustes.obtener("guionista") is None
    guionista.elegir("claude")
    guionista.ultimo_error = "claude: la API respondió 500"
    guionista.estan["ollama"] = False
    respuesta = await con_guionista("GET", "/api/guionista", guionista)
    assert respuesta.status == 200
    assert cuerpo(respuesta) == {
        "activo": "claude",
        "disponibles": {"frases": True, "claude": True, "ollama": False},
        "ultimo_error": "claude: la API respondió 500",
        "modelos": {"claude": "claude-opus-5-5", "ollama": "llama3.2:3b"},
    }
    assert guionista.sesiones == ["la sesión", "la sesión"]

@pytest.mark.parametrize("nombre", ["claude", "ollama"])
async def test_post_guionista_cambia_en_caliente_guarda_la_eleccion_y_es_la_de_la_proxima_vez(guionista, nombre):
    respuesta = await con_guionista("POST", f"/api/guionista?nombre={nombre}", guionista)
    assert respuesta.status == 200 and cuerpo(respuesta)["activo"] == nombre
    assert guionista.activo == nombre and ajustes.obtener("guionista") == nombre
    assert Guionista().activo == nombre

@pytest.mark.parametrize("ruta", [
    "/api/guionista?nombre=gpt", "/api/guionista", "/api/guionista?nombre=", "/api/guionista?nombre=Claude",
    "/api/guionista?nombre=../../etc",
])
async def test_post_guionista_con_un_nombre_que_no_existe_es_400(guionista, ruta):
    guionista.elegir("ollama")
    respuesta = await con_guionista("POST", ruta, guionista)
    assert respuesta.status == 400 and "desconocido" in cuerpo(respuesta)["error"]
    assert guionista.activo == "ollama" and ajustes.obtener("guionista") is None

@pytest.mark.parametrize("nombre", ["claude", "ollama"])
async def test_post_de_un_motor_que_no_esta_disponible_es_409_y_no_cambia_nada(guionista, nombre):
    guionista.estan[nombre] = False
    respuesta = await con_guionista("POST", f"/api/guionista?nombre={nombre}", guionista)
    assert respuesta.status == 409 and nombre in cuerpo(respuesta)["error"]
    assert guionista.activo == "frases" and ajustes.obtener("guionista") is None

async def test_las_frases_siempre_se_pueden_elegir_aunque_nada_este_disponible(guionista):
    guionista.elegir("claude")
    guionista.estan = {"frases": False, "claude": False, "ollama": False}
    respuesta = await con_guionista("POST", "/api/guionista?nombre=frases", guionista)
    assert respuesta.status == 200 and cuerpo(respuesta)["activo"] == "frases"
    assert guionista.activo == "frases" and ajustes.obtener("guionista") == "frases"

async def test_si_no_se_puede_guardar_la_eleccion_es_500_y_no_cambia_nada(guionista, monkeypatch):
    def falla(**_):
        raise OSError("disco lleno")

    monkeypatch.setattr(ajustes, "guardar", falla)
    respuesta = await con_guionista("POST", "/api/guionista?nombre=claude", guionista)
    assert respuesta.status == 500 and "disco lleno" in cuerpo(respuesta)["error"]
    assert guionista.activo == "frases"

@pytest.mark.parametrize("metodo, ruta", [("GET", "/api/guionista"), ("POST", "/api/guionista?nombre=claude")])
async def test_guionista_sin_cog_es_503(guionista, metodo, ruta):
    respuesta = await con_guionista(metodo, ruta, guionista, bot=FakeBot(None))
    assert respuesta.status == 503 and ajustes.obtener("guionista") is None

async def test_las_rutas_del_guionista_existen_y_pasan_por_la_proteccion(guionista, monkeypatch):
    async def disponibles(self, session=None):
        return dict(guionista.estan)

    monkeypatch.setattr(Guionista, "disponibles", disponibles)

    async def prueba(cliente, puerto, app):
        r = await cliente.get("/api/guionista")
        assert r.status == 200 and (await r.json())["activo"] == "frases"
        await _bloqueadas(cliente, gets=["/api/guionista"], posts=["/api/guionista?nombre=claude"])
        assert ajustes.obtener("guionista") is None
        r = await cliente.post("/api/guionista?nombre=claude", headers={"Origin": f"http://127.0.0.1:{puerto}"})
        assert r.status == 200 and ajustes.obtener("guionista") == "claude"

    await _con_servidor(prueba)

# --- panel web y log --------------------------------------------------------

async def test_la_pagina_del_panel_se_sirve_con_sus_secciones():
    respuesta = await llamar("handle_pagina", "GET", "/")
    assert respuesta.content_type == "text/html" and "LavaDJ" in respuesta.text
    for pieza in (
        "esc(", "api('voces')", "voces/descargar", "voces/borrar", "voz/filtro", "voces/muestra",
        "api('guionista", "Guionista del DJ", "Frases fijas", "Claude (API, cobra por uso)", "Ollama (local)",
        "api('letra?servidor=", "<h2>Letra</h2>", "requestAnimationFrame", "performance.now()", "localStorage", "esc(l.texto)",
    ):
        assert pieza in respuesta.text, pieza

async def test_el_log_devuelve_las_ultimas_lineas_y_sin_archivo_viene_vacio(tmp_path, monkeypatch):
    archivo = tmp_path / "bot.log"
    monkeypatch.setattr(api, "LOG_FILE", archivo)
    assert cuerpo(await llamar("handle_log", "GET", "/api/log"))["lineas"] == []
    archivo.write_text("\n".join(f"linea {i}" for i in range(10)), encoding="utf-8")
    assert cuerpo(await llamar("handle_log", "GET", "/api/log?lineas=3"))["lineas"] == ["linea 7", "linea 8", "linea 9"]

# --- protección contra CSRF y DNS rebinding ---------------------------------

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
        for origen in ("http://evil.example.com", "null", f"http://127.0.0.1:{puerto + 1}", f"https://127.0.0.1:{puerto}"):
            r = await cliente.post("/api/stop", headers={"Origin": origen})
            assert r.status == 403, origen
        assert not app["apagar"].is_set()

    await _con_servidor(prueba)

@pytest.mark.parametrize("con_origin", [True, False], ids=["origin propio", "sin origin (el panel GTK no lo manda)"])
async def test_post_con_origin_propio_o_sin_origin_funciona(con_origin):
    async def prueba(cliente, puerto, app):
        r = await cliente.post("/api/stop", headers={"Origin": f"http://127.0.0.1:{puerto}"} if con_origin else {})
        assert r.status == 200 and app["apagar"].is_set()

    await _con_servidor(prueba)

@pytest.mark.parametrize("query, todo", [
    ("", False), ("?motivo=panel+web", False), ("?todo=0", False), ("?todo=si", False),
    ("?todo=1", True), ("?motivo=panel+web&todo=1", True),
])
async def test_stop_siempre_pide_el_cierre_y_solo_con_todo_deja_anotado_bajar_lavalink(query, todo):
    bot = FakeBot(FakeCog())
    peticion = pedido("POST", "/api/stop" + query, bot)
    respuesta = await api.handle_stop(peticion)
    assert respuesta.status == 200 and cuerpo(respuesta) == {"status": "stopping", "todo": todo}
    assert peticion.app["apagar"].is_set() and bot.apagar_todo is todo

async def test_un_stop_sin_todo_posterior_no_borra_lo_anotado():
    bot = FakeBot(FakeCog())
    for query in ("?todo=1", ""):
        await api.handle_stop(pedido("POST", "/api/stop" + query, bot))
    assert bot.apagar_todo is True

async def test_stop_con_todo_no_hace_nada_si_el_origen_es_ajeno():
    async def prueba(cliente, puerto, app):
        r = await cliente.post("/api/stop?todo=1", headers=EVIL_ORIGIN)
        assert r.status == 403 and not app["apagar"].is_set() and app["bot"].apagar_todo is False
        r = await cliente.post("/api/stop?todo=1")
        assert r.status == 200 and app["apagar"].is_set() and app["bot"].apagar_todo is True

    await _con_servidor(prueba)

async def test_las_rutas_de_voces_existen_y_pasan_por_la_proteccion(sintesis):
    async def prueba(cliente, puerto, app):
        r = await cliente.get("/api/voces")
        assert r.status == 200 and "voces" in await r.json()
        r = await cliente.get("/api/voces/muestra?voz=espeak")
        assert r.status == 200 and r.content_type == "audio/wav"
        await _bloqueadas(
            cliente, gets=["/api/voces", "/api/voces/muestra?voz=espeak"],
            posts=["/api/voz?voz=espeak", "/api/voz/filtro?activo=0", f"/api/voces/descargar?voz={ALD}", f"/api/voces/borrar?voz={ALD}"],
        )
        assert ajustes.obtener("filtro_radio") is True
        r = await cliente.post("/api/voz/filtro?activo=0", headers={"Origin": f"http://127.0.0.1:{puerto}"})
        assert r.status == 200 and ajustes.obtener("filtro_radio") is False

    await _con_servidor(prueba)
