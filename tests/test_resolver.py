"""El resolver: yt-dlp saca la URL, Lavalink la reproduce.

Lo que se protege aquí es que la pista que termina sonando se siga *viendo* como la de YouTube
(el panel y el DJ leen esos datos) y que ningún tropiezo de yt-dlp tumbe la reproducción:
ante la duda, se devuelve la pista original.
"""

import asyncio
import gc
import os
import sys
import time
from datetime import date
from types import SimpleNamespace

import pytest
import wavelink

from core import resolver
from core.sponsorblock import Segmento, SponsorBlock

URL_HTTP = "https://rr4.googlevideo.com/videoplayback?x=1"
URL_DE_AUDIO = "https://rr4.googlevideo.com/v"
ERROR_PASAJERO = b"ERROR: unable to download video data: HTTP Error 400: Bad Request"
ERROR_DEFINITIVO = b"ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader"
ERROR_BLOQUEADO = b"ERROR: [youtube] abc: It was blocked due to the claimed content by Paramount Global (PMN)."
CLIENTES = ("android", "android_vr", "ios", "mweb")
INTRO = Segmento(0, 12_500, 200_000)
DEL_MEDIO = Segmento(90_000, 100_000, 200_000)

@pytest.fixture
def res():
    return resolver.Resolver()

@pytest.fixture
def hoy(monkeypatch):
    """Día fijo: `2026.07.04` es viejo (110 días) y `2026.10.20` no."""
    monkeypatch.setattr(resolver, "_hoy", lambda: date(2026, 10, 22))

class FakePlayable:
    """Lo mínimo que resolver.py le pide a un wavelink.Playable."""

    def __init__(self, source="youtube", uri="https://www.youtube.com/watch?v=abc",
                 title="Un tema", author="Alguien", identifier="abc"):
        self.source, self.uri, self.title, self.identifier, self.length = source, uri, title, identifier, 200_000
        self.raw_data = {
            "encoded": f"encoded-{identifier}",
            "info": {
                "title": title, "author": author, "identifier": identifier, "uri": uri,
                "artworkUrl": "https://img/portada.jpg", "length": 200_000, "isSeekable": True,
                "isStream": False, "position": 0, "sourceName": source,
            },
            "pluginInfo": {},
            "userData": {},
        }

    @property
    def extras(self):
        """Como en wavelink: `userData` se lee de vuelta por aquí."""
        return self.raw_data["userData"]

def pista_http():
    p = FakePlayable(source="http", uri=URL_HTTP, title="Unknown title", author="Unknown artist", identifier="rr4-loquesea")
    p.raw_data["encoded"] = "encoded-http"
    return p

def pista_ya_resuelta():
    """La pista http como vuelve de Lavalink en un evento de excepción: lo único que la ata a YouTube es el `userData`."""
    p = pista_http()
    p.raw_data["userData"] = {resolver.CLAVE_ORIGEN: {
        "uri": "https://www.youtube.com/watch?v=abc", "identifier": "abc", "title": "Un tema",
        "author": "Alguien", "artworkUrl": "https://img/portada.jpg",
    }}
    return p

def _de_youtube(id_):
    return FakePlayable(uri=f"https://www.youtube.com/watch?v={id_}", title=f"Tema {id_}", identifier=id_)

def _fusionar(monkeypatch):
    """Corre la fusión de metadatos sin construir un wavelink.Playable real."""
    capturado = {}
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: capturado.setdefault("d", datos))
    resolver._con_la_cara_del_original(pista_http(), FakePlayable())
    return capturado["d"]

def _url_que_vence_en(segundos):
    return f"https://rr4.googlevideo.com/videoplayback?itag=18&expire={int(time.time() + segundos)}&x=1"

def _fetch_que(cargas, fallan=0, vacio=False):
    """`Pool.fetch_tracks` de mentira: anota cada carga, falla las primeras `fallan` y con `vacio` no encuentra nada."""
    async def _fetch(identifier):
        cargas.append(identifier)
        if len(cargas) <= fallan and not vacio:
            raise RuntimeError("Read timed out")
        return [] if vacio else [pista_http()]
    return _fetch

def _dobles(res, monkeypatch, url=None, frenar=None, respuestas=None, fetch=None):
    """yt-dlp y Lavalink de mentira; devuelve las páginas que se le pidieron a yt-dlp.

    Con `frenar` (un `asyncio.Event`) yt-dlp espera a que lo suelten; `respuestas` es una cola de URLs (o None).
    """
    pedidas = []

    async def _url(pagina):
        pedidas.append(pagina)
        if frenar is not None:
            await frenar.wait()
        return respuestas.pop(0) if respuestas else url or _url_que_vence_en(6 * 3600)

    monkeypatch.setattr(res, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", fetch or _fetch_que([]))
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: datos)
    return pedidas

async def _esperar_precargas(res):
    tareas = list(res._precargas.values())
    if tareas:
        await asyncio.wait(tareas)

async def _precargar_y_resolver(res, pista):
    res.precargar(pista)
    await _esperar_precargas(res)
    return await res.resolve(pista)

class _Proceso:
    """Proceso hijo de mentira; con `frenar` no contesta hasta que lo suelten y con `demora` tarda esos segundos."""

    def __init__(self, salida=b"", error=b"", codigo=0, frenar=None, al_terminar=None, demora=0):
        self.salida, self.error, self.returncode = salida, error, codigo
        self.frenar, self.al_terminar, self.demora = frenar, al_terminar, demora
        self.matado = False

    async def communicate(self):
        if self.frenar is not None:
            await self.frenar.wait()
        if self.demora:
            await asyncio.sleep(self.demora)
        if self.al_terminar is not None:
            self.al_terminar()
        return self.salida, self.error

    def kill(self):
        self.matado = True

    async def wait(self):
        return self.returncode

def _falso_proceso(monkeypatch, salida=b"", error=b"", codigo=0, **kwargs):
    """Todo `create_subprocess_exec` devuelve el mismo `_Proceso`; el resultado trae `args` (los últimos) y `proceso`."""
    capturado = {"proceso": _Proceso(salida, error, codigo, **kwargs)}

    async def _exec(*args, **kw):
        capturado["args"] = list(args)
        return capturado["proceso"]

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)
    return capturado

def _no_lanzable(monkeypatch, error=FileNotFoundError("no existe")):
    async def _exec(*args, **kwargs):
        raise error

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)

class Reloj:
    """La hora monotónica de mentira: solo avanza si el test la mueve."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

class YtDlpFalso:
    """Un `create_subprocess_exec` que contesta según lo que le pidan.

    `--get-url` da una URL si el cliente está en `funcionan` y un error pasajero si no;
    `--version` da `version`; cualquier otra cosa es la actualización.
    """

    def __init__(self, funcionan=(), *, version="2026.10.01", actualizacion=0, error=ERROR_PASAJERO,
                 error_de_prueba=None, colgar=(), frenar_pruebas=None, frenar_pedidas=None,
                 frenar_actualizacion=None, al_actualizar=None):
        self.funcionan, self.version, self.actualizacion = set(funcionan), version, actualizacion
        self.error, self.error_de_prueba, self.colgar = error, error_de_prueba, set(colgar)
        self.frenar_pruebas, self.frenar_pedidas = frenar_pruebas, frenar_pedidas
        self.frenar_actualizacion, self.al_actualizar = frenar_actualizacion, al_actualizar
        self.llamadas, self.orden, self.pedidas, self.actualizaciones, self.procesos = [], [], [], [], []

    @property
    def pruebas(self):
        return [c for c, url in self.pedidas if url == resolver.VIDEO_DE_PRUEBA]

    @property
    def versiones_pedidas(self):
        return self.orden.count("version")

    def de(self, tipo):
        return [p for t, p in self.procesos if t == tipo]

    def _terminar_actualizacion(self):
        self.orden.append("actualizada")
        if self.al_actualizar is not None:
            self.al_actualizar(self)

    async def __call__(self, *args, **kwargs):
        self.llamadas.append(list(args))
        if "--get-url" in args:
            tipo = "pedir"
            cliente, url = args[args.index("--extractor-args") + 1].split("=", 1)[1], args[-1]
            self.pedidas.append((cliente, url))
            es_prueba = url == resolver.VIDEO_DE_PRUEBA
            frenar = self.frenar_pruebas if es_prueba else self.frenar_pedidas
            if cliente in self.colgar:
                proceso = _Proceso(frenar=asyncio.Event())
            elif cliente in self.funcionan:
                proceso = _Proceso(salida=(URL_DE_AUDIO + "\n").encode(), frenar=frenar)
            else:
                error = self.error_de_prueba if es_prueba and self.error_de_prueba else self.error
                proceso = _Proceso(error=error, codigo=1, frenar=frenar)
        elif "--version" in args:
            tipo = "version"
            proceso = _Proceso(salida=(self.version + "\n").encode())
        else:
            tipo = "actualizar"
            self.actualizaciones.append(list(args))
            proceso = _Proceso(
                salida=b"Successfully installed yt-dlp", codigo=self.actualizacion, frenar=self.frenar_actualizacion,
                al_terminar=self._terminar_actualizacion if self.actualizacion == 0 else None,
            )
        self.orden.append(tipo)
        self.procesos.append((tipo, proceso))
        return proceso

def _yt_dlp(monkeypatch, funcionan=(), **kwargs):
    falso = YtDlpFalso(funcionan, **kwargs)
    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", falso)
    return falso

def _resolver_que_rota(**kwargs):
    """Cuatro clientes, sin autoactualizar y con reloj propio; el binario no es el del venv, así que actualizar es `-U`."""
    kwargs.setdefault("cliente", "android")
    kwargs.setdefault("clientes", CLIENTES)
    kwargs.setdefault("autoactualizar", False)
    kwargs.setdefault("reloj", Reloj())
    return resolver.Resolver(binario="/otro/yt-dlp", **kwargs)

async def _fallar(res, veces, url="https://www.youtube.com/watch?v=abc"):
    for _ in range(veces):
        assert await res.direct_url(url) is None

async def _dejar_correr():
    for _ in range(30):
        await asyncio.sleep(0)

async def _terminar_sondeo(res):
    if res._sondeo is not None:
        await res._sondeo

async def _cancelar(tarea):
    """Cancela hasta que muere: en 3.11 `wait_for` puede tragarse una cancelación que llega justo al terminar."""
    while not tarea.done():
        tarea.cancel()
        await asyncio.sleep(0.001)
    with pytest.raises(asyncio.CancelledError):
        await tarea

def _player_con(monkeypatch, sb=None, reanudar=None, conectado=True):
    """Un `ResolvingPlayer` sin red; devuelve los kwargs con que llega a `Player.play`. `vc.visto` anota `resolviendo`."""
    vc = resolver.ResolvingPlayer.__new__(resolver.ResolvingPlayer)
    vc.resolver = resolver.Resolver(sponsorblock=sb) if sb is not None else resolver.Resolver()
    vc.reanudar, vc.resolviendo, vc.arranque_ms, vc.visto = reanudar or {}, 0, 0, []
    recibidos = []

    async def _resolve(pista):
        vc.visto.append(vc.resolviendo)
        return pista

    async def _play(self, pista, *a, **kw):
        vc.visto.append(vc.resolviendo)
        recibidos.append(kw)
        return pista

    monkeypatch.setattr(vc.resolver, "resolve", _resolve)
    monkeypatch.setattr(wavelink.Player, "play", _play)
    monkeypatch.setattr(wavelink.Player, "connected", property(lambda self: conectado))
    return vc, recibidos

class SBQueAnota(SponsorBlock):
    """SponsorBlock sin red: responde con `respuestas` y anota por qué videos le preguntaron."""

    def __init__(self, respuestas=None):
        super().__init__(activo=True)
        self.respuestas = respuestas or {}
        self.preguntados = []

    async def _traer(self, video):
        self.preguntados.append(video)
        self._cache[video] = tuple(self.respuestas.get(video, ()))
        return self._cache[video]

# --- la cara de la pista -----------------------------------------------------

def test_la_pista_que_suena_conserva_el_encoded_del_stream_y_muestra_los_datos_de_youtube(monkeypatch):
    hecha = _fusionar(monkeypatch)
    assert hecha["encoded"] == "encoded-http"      # es lo que Lavalink toca
    info = hecha["info"]
    assert (info["title"], info["author"], info["artworkUrl"]) == ("Un tema", "Alguien", "https://img/portada.jpg")
    # El identifier es la clave de los reintentos del cog (no el de googlevideo, que cambia en cada resolución)
    # y la fuente se presenta como youtube: con "http" el autoplay de wavelink no tiene semilla.
    assert info["identifier"] == "abc" and info["sourceName"] == "youtube"
    # Lo único que sobrevive el viaje a Lavalink y vuelta es `userData`.
    origen = hecha["userData"]["yt_origen"]
    assert {k: origen[k] for k in ("uri", "identifier", "title", "author", "artworkUrl")} == {
        "uri": "https://www.youtube.com/watch?v=abc", "identifier": "abc", "title": "Un tema",
        "author": "Alguien", "artworkUrl": "https://img/portada.jpg"}

# --- resolve -----------------------------------------------------------------

async def test_lo_que_no_es_de_youtube_pasa_derecho(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch)
    clip = FakePlayable(source="local", uri="/tmp/dj_abc.wav")
    assert await res.resolve(clip) is clip and pedidas == []

@pytest.mark.parametrize("caso", ["sin_url", "vacio", "falla"])
async def test_si_yt_dlp_o_lavalink_no_pueden_se_devuelve_la_original(res, monkeypatch, caso):
    monkeypatch.setattr(resolver, "PAUSA_ENTRE_CARGAS", 0)
    cargas = []
    _dobles(res, monkeypatch, url=URL_HTTP, respuestas=[None] if caso == "sin_url" else None,
            fetch=_fetch_que(cargas, fallan=99, vacio=caso == "vacio"))
    pista = FakePlayable()
    assert await res.resolve(pista) is pista
    if caso == "sin_url":
        assert cargas == []
    if caso == "falla":
        assert len(cargas) == resolver.INTENTOS_DE_CARGA

async def test_si_lavalink_falla_una_vez_se_reintenta_con_la_misma_url(res, monkeypatch):
    """Un tropiezo suelto de Lavalink con la URL no debe mandar al plugin."""
    monkeypatch.setattr(resolver, "PAUSA_ENTRE_CARGAS", 0)
    cargas = []
    _dobles(res, monkeypatch, url=URL_HTTP, fetch=_fetch_que(cargas, fallan=1))
    pista = FakePlayable()
    assert await res.resolve(pista) is not pista
    assert cargas == [URL_HTTP] * 2

async def test_el_reintento_pide_una_url_nueva_con_la_uri_de_youtube_y_no_pierde_la_cara(res, monkeypatch):
    """Sin esto se repetía la URL ya muerta y, a la segunda vuelta, el panel mostraba "Unknown title" para siempre."""
    pedidas = _dobles(res, monkeypatch, url="https://rr9.googlevideo.com/videoplayback?fresca=1")
    datos = await res.resolve(pista_ya_resuelta())
    assert pedidas == ["https://www.youtube.com/watch?v=abc"]
    assert (datos["info"]["title"], datos["info"]["identifier"]) == ("Un tema", "abc")
    assert datos["userData"][resolver.CLAVE_ORIGEN]["uri"] == "https://www.youtube.com/watch?v=abc"      # aguanta todas las vueltas

# --- direct_url --------------------------------------------------------------

async def test_direct_url_se_queda_con_la_linea_de_la_url_y_da_none_si_yt_dlp_falla(res, monkeypatch):
    _falso_proceso(monkeypatch, salida=b"algo de ruido\nhttps://rr4.googlevideo.com/v?x=1\n")
    assert await res.direct_url("https://youtu.be/abc") == "https://rr4.googlevideo.com/v?x=1"
    _falso_proceso(monkeypatch, error=b"ERROR: Sign in to confirm", codigo=1)
    assert await res.direct_url("https://youtu.be/abc") is None

async def test_direct_url_sin_url_no_llama_a_nada(res, monkeypatch):
    def _no_llamar(*a, **kw):
        raise AssertionError("no había que lanzar yt-dlp")

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _no_llamar)
    assert await res.direct_url("") is None

async def test_el_texto_va_como_argumento_nunca_por_shell(res, monkeypatch):
    """Las URLs vienen de YouTube: si pasaran por una shell serían inyectables."""
    capturado = _falso_proceso(monkeypatch, salida=b"https://rr4.googlevideo.com/v\n")
    await res.direct_url("https://youtu.be/abc; rm -rf /")
    assert capturado["args"][-1] == "https://youtu.be/abc; rm -rf /"
    assert capturado["args"][0] == resolver.config.YTDLP_BIN

async def test_cancelar_direct_url_mata_a_yt_dlp(res, monkeypatch):
    """Una precarga descartada se cancela a mitad: yt-dlp no puede quedar huérfano."""
    proceso = _falso_proceso(monkeypatch, frenar=asyncio.Event())["proceso"]
    tarea = asyncio.create_task(res.direct_url("https://youtu.be/abc"))
    await _dejar_correr()
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert proceso.matado

@pytest.mark.parametrize("error", [FileNotFoundError("no existe"), PermissionError("sin permiso")])
async def test_un_yt_dlp_que_no_se_puede_lanzar_da_none_cae_a_la_original_y_no_cuenta_contra_el_cliente(monkeypatch, error):
    """Un OSError al lanzar yt-dlp no puede escapar: en el autoplay mataría la cola."""
    _no_lanzable(monkeypatch, error)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR + 2)
    pista = FakePlayable()
    assert await res.resolve(pista) is pista
    assert res._fallos_seguidos == 0 and res._sondeo is None

@pytest.mark.parametrize("error, pista, esperado", [
    (ERROR_BLOQUEADO, FakePlayable, "está bloqueada"),      # reintentar un video bloqueado no sirve nunca
    (ERROR_PASAJERO, FakePlayable, None),      # un 400 o un timeout sí mejoran reintentando
    (ERROR_DEFINITIVO, pista_ya_resuelta, "la borraron"),      # en el reintento la pista trae la uri de googlevideo
])
async def test_un_video_sin_arreglo_queda_marcado_como_definitivo_y_un_tropiezo_pasajero_no(res, monkeypatch, error, pista, esperado):
    _falso_proceso(monkeypatch, error=error, codigo=1)
    await res.direct_url("https://www.youtube.com/watch?v=abc")
    assert res.motivo_definitivo(pista()) == esperado

async def test_cada_resolver_tiene_su_tabla_de_videos_sin_caso_y_se_puede_vaciar(res, monkeypatch):
    _falso_proceso(monkeypatch, error=b"ERROR: [youtube] abc: Private video. Sign in if you've been granted access", codigo=1)
    await res.direct_url("https://www.youtube.com/watch?v=abc")
    assert res.motivo_definitivo(FakePlayable()) == "es privada"
    assert resolver.Resolver().motivo_definitivo(FakePlayable()) is None
    res.olvidar_motivos()
    assert res.motivo_definitivo(FakePlayable()) is None

async def test_la_tabla_de_videos_sin_caso_esta_acotada(res, monkeypatch):
    """Es una ayuda para no quemar reintentos, no un registro histórico."""
    monkeypatch.setattr(resolver, "RECUERDO_MAXIMO", 2)
    _falso_proceso(monkeypatch, error=ERROR_DEFINITIVO, codigo=1)
    for letra in "abc":
        await res.direct_url(f"https://www.youtube.com/watch?v={letra}")

    def visto(letra):
        return res.motivo_definitivo(FakePlayable(uri=f"https://www.youtube.com/watch?v={letra}"))

    assert visto("a") is None       # la más vieja se cayó
    assert visto("b") == visto("c") == "la borraron"

# --- yt-dlp viejo y su estado --------------------------------------------------

def test_la_version_de_yt_dlp_dice_cuantos_dias_tiene_y_una_que_no_es_fecha_no_rompe_nada():
    hoy = date(2026, 8, 28)
    assert resolver.antiguedad_de("2026.07.04", hoy=hoy) == 55
    assert resolver.antiguedad_de("2026.07.04.dev0+g1a2b3c", hoy=hoy) == 55
    assert resolver.antiguedad_de("vaya uno a saber", hoy=hoy) is None
    assert resolver.antiguedad_de("", hoy=hoy) is None

async def test_revisar_ytdlp_deja_el_estado_a_mano_del_panel_y_no_lo_comparte_con_otro_resolver(monkeypatch, hoy):
    _yt_dlp(monkeypatch, version="2026.07.04")
    res, otro = _resolver_que_rota(), resolver.Resolver()
    await res.revisar_ytdlp()
    estado = res.estado_ytdlp()
    assert (estado["version"], estado["dias"], estado["viejo"]) == ("2026.07.04", 110, True)
    assert (otro.estado_ytdlp()["version"], otro.estado_ytdlp()["viejo"]) == (None, False)

def test_el_estado_de_yt_dlp_trae_el_cliente_actual_y_el_configurado_y_se_entrega_como_copia():
    res = resolver.Resolver(cliente="android")
    assert res.estado_ytdlp() == {
        "version": None, "dias": None, "viejo": False, "cliente": "android", "cliente_configurado": "android",
    }
    res.estado_ytdlp()["version"] = "tocada"      # quien lo lee (la API) no puede pisarle el estado al resolver
    assert res.estado_ytdlp()["version"] is None
    res.cliente = "android_vr"
    assert (res.estado_ytdlp()["cliente"], res.estado_ytdlp()["cliente_configurado"]) == ("android_vr", "android")

# --- el player ---------------------------------------------------------------

@pytest.mark.parametrize("conectado, visto, tocadas", [(True, [1, 1], [{}]), (False, [1], [])])
async def test_play_avisa_que_resuelve_y_no_toca_lavalink_si_el_player_se_desconecto(monkeypatch, conectado, visto, tocadas):
    """`resolviendo` impide que el cog largue otro tema encima; y sin conexión el PATCH recrearía un player huérfano."""
    vc, recibidos = _player_con(monkeypatch, conectado=conectado)
    pista = FakePlayable()
    assert await vc.play(pista) is (pista if conectado else None)
    assert (vc.visto, recibidos, vc.resolviendo) == (visto, tocadas, 0)

def _nodo_de_mentira():
    """Lo mínimo del nodo que `wavelink.Player.__init__` lee al construirse."""
    return SimpleNamespace(players={}, client=None, _inactive_channel_tokens=None, _inactive_player_timeout=None)

def test_el_player_toma_el_resolver_del_bot_o_arma_el_suyo():
    del_bot = resolver.Resolver()
    vc = resolver.ResolvingPlayer(SimpleNamespace(resolver=del_bot), "canal-de-voz", nodes=[_nodo_de_mentira()])
    assert vc.resolver is del_bot
    uno = resolver.ResolvingPlayer(SimpleNamespace(), "canal-de-voz", nodes=[_nodo_de_mentira()])
    otro = resolver.ResolvingPlayer(SimpleNamespace(), "canal-de-voz", nodes=[_nodo_de_mentira()])
    assert isinstance(uno.resolver, resolver.Resolver) and uno.resolver is not otro.resolver

# --- el estado vive en la instancia ------------------------------------------

def test_sin_argumentos_el_resolver_toma_la_configuracion_al_construirse(monkeypatch):
    monkeypatch.setattr(resolver.config, "YTDLP_BIN", "/otro/yt-dlp")
    monkeypatch.setattr(resolver.config, "YTDLP_TIMEOUT", 7)
    monkeypatch.setattr(resolver.config, "YTDLP_PLAYER_CLIENT", "mweb")
    monkeypatch.setattr(resolver.config, "YTDLP_CLIENTES", ("ios", "web"))
    monkeypatch.setattr(resolver.config, "YTDLP_AUTOACTUALIZAR", False)
    res = resolver.Resolver()
    assert (res.binario, res.timeout, res.cliente, res.clientes, res.autoactualizar) == ("/otro/yt-dlp", 7, "mweb", ("ios", "web"), False)
    # Se lee al construir, no en cada llamada (para cambiarlo en caliente está `res.cliente`).
    monkeypatch.setattr(resolver.config, "YTDLP_PLAYER_CLIENT", "ios")
    monkeypatch.setattr(resolver.config, "YTDLP_AUTOACTUALIZAR", True)
    assert res.cliente == "mweb" and res.autoactualizar is False

def test_los_argumentos_del_constructor_ganan_a_la_configuracion(monkeypatch):
    monkeypatch.setattr(resolver.config, "YTDLP_AUTOACTUALIZAR", False)
    res = resolver.Resolver(binario="/opt/yt-dlp", cliente="ios", timeout=5, clientes=["web"], autoactualizar=True)
    assert (res.binario, res.timeout, res.cliente, res.clientes, res.autoactualizar) == ("/opt/yt-dlp", 5, "ios", ("web",), True)

async def test_el_cliente_se_puede_cambiar_en_caliente_y_el_binario_del_constructor_es_el_que_se_lanza(monkeypatch):
    """El argumento se arma en cada llamada: rotar de cliente no puede exigir reiniciar el bot."""
    res = resolver.Resolver(binario="/opt/yt-dlp/yt-dlp", cliente="android")
    capturado = _falso_proceso(monkeypatch, salida=b"https://rr4.googlevideo.com/v\n")

    def extractor():
        args = capturado["args"]
        return args[args.index("--extractor-args") + 1]

    await res.direct_url("https://youtu.be/abc")
    assert extractor() == "youtube:player_client=android" and capturado["args"][0] == "/opt/yt-dlp/yt-dlp"
    res.cliente = "ios"
    await res.direct_url("https://youtu.be/abc")
    assert extractor() == "youtube:player_client=ios"
    capturado = _falso_proceso(monkeypatch, salida=b"2026.07.04\n")
    await res.revisar_ytdlp()
    assert capturado["args"][0] == "/opt/yt-dlp/yt-dlp"

@pytest.mark.parametrize("demora, timeout, esperado, matado", [(10, 0.05, None, True), (0.05, 5, URL_DE_AUDIO, False)])
async def test_el_timeout_del_constructor_corta_a_yt_dlp_solo_si_se_pasa(monkeypatch, demora, timeout, esperado, matado):
    proceso = _falso_proceso(monkeypatch, salida=b"https://rr4.googlevideo.com/v\n", demora=demora)["proceso"]
    assert await resolver.Resolver(timeout=timeout).direct_url("https://youtu.be/abc") == esperado
    assert proceso.matado is matado

# --- precargar la que viene ----------------------------------------------------

async def test_resolve_usa_la_url_precargada_una_sola_vez(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch)
    pista = FakePlayable()
    datos = await _precargar_y_resolver(res, pista)
    assert pedidas == [pista.uri]
    assert (datos["encoded"], datos["info"]["title"]) == ("encoded-http", "Un tema")
    assert datos["userData"][resolver.CLAVE_ORIGEN]["identifier"] == "abc"
    assert res._precargas == {}
    # Sin precarga: una sola pasada por yt-dlp como siempre, y en un reintento no vuelve la misma URL.
    assert (await res.resolve(pista))["encoded"] == "encoded-http"
    assert pedidas == [pista.uri, pista.uri]

async def test_resolve_espera_a_la_precarga_que_sigue_en_curso(res, monkeypatch):
    """Un skip apurado llega con yt-dlp todavía trabajando: no se lanza otro."""
    frenar = asyncio.Event()
    pedidas = _dobles(res, monkeypatch, frenar=frenar)
    pista = FakePlayable()
    res.precargar(pista)
    await asyncio.sleep(0)           # la tarea arranca y queda esperando a yt-dlp
    resolviendo = asyncio.create_task(res.resolve(pista))
    await asyncio.sleep(0)
    assert not resolviendo.done()
    frenar.set()
    datos = await resolviendo
    assert pedidas == [pista.uri] and datos["encoded"] == "encoded-http"

@pytest.mark.parametrize("vence_en, largo_ms, pedidas_esperadas", [
    (-60, 200_000, 2),      # vencida: se descarta
    (200 + resolver.MARGEN_DE_VENCIMIENTO - 30, 200_000, 2),      # no cubre la duración más el margen
    (200 + resolver.MARGEN_DE_VENCIMIENTO + 60, 200_000, 1),
    (resolver.MARGEN_DE_VENCIMIENTO + 60, 0, 1),      # sin duración conocida basta el margen
    (None, 200_000, 1),      # sin `expire` vive `VIDA_SIN_EXPIRE`: sirve para un tema corto...
    (None, (resolver.VIDA_SIN_EXPIRE + 60) * 1000, 2),      # ...no para uno largo
])
async def test_la_precarga_solo_vale_si_la_url_cubre_la_duracion_y_el_margen(res, monkeypatch, vence_en, largo_ms, pedidas_esperadas):
    url = _url_que_vence_en(vence_en) if vence_en is not None else "https://rr4.googlevideo.com/videoplayback?itag=18&x=1"
    pedidas = _dobles(res, monkeypatch, url=url)
    pista = FakePlayable()
    pista.length = largo_ms
    await _precargar_y_resolver(res, pista)
    assert len(pedidas) == pedidas_esperadas

@pytest.mark.parametrize("falla", ["yt-dlp", "lavalink"])
async def test_si_no_se_pudo_precargar_se_resuelve_por_el_camino_normal(res, monkeypatch, falla):
    monkeypatch.setattr(resolver, "PAUSA_ENTRE_CARGAS", 0)
    pedidas = _dobles(
        res, monkeypatch, respuestas=[None, _url_que_vence_en(6 * 3600)] if falla == "yt-dlp" else None,
        fetch=_fetch_que([], fallan=resolver.INTENTOS_DE_CARGA if falla == "lavalink" else 0),
    )
    datos = await _precargar_y_resolver(res, FakePlayable())
    assert len(pedidas) == 2 and datos["encoded"] == "encoded-http"

async def test_una_precarga_que_revienta_no_escapa_como_tarea_no_recuperada(res, monkeypatch, caplog):
    """Nadie mira la tarea hasta que le toca sonar, y a lo mejor nunca."""
    sin_atender = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, contexto: sin_atender.append(contexto))
    pedidas = _dobles(res, monkeypatch)
    original, llamadas = res._cargar, []

    async def _cargar_que_revienta(pagina, titulo):
        llamadas.append(pagina)
        if len(llamadas) == 1:
            raise RuntimeError("algo que nadie previó")
        return await original(pagina, titulo)

    monkeypatch.setattr(res, "_cargar", _cargar_que_revienta)
    pista = FakePlayable()
    with caplog.at_level("WARNING", logger="resolver"):
        res.precargar(pista)
        await _esperar_precargas(res)
        res._precargas.clear()      # se suelta la tarea sin consumirla: es el caso que antes se perdía
        gc.collect()
        await asyncio.sleep(0)
    assert sin_atender == [] and "Falló la precarga" in caplog.text
    assert (await _precargar_y_resolver(res, pista))["encoded"] == "encoded-http"      # suena igual
    assert len(pedidas) == 1

async def test_la_precarga_se_muestra_con_la_cara_de_la_pista_actual_sin_pisar_la_guardada(res, monkeypatch):
    """Entre precargar y sonar la pista pudo cambiar: la cara no sale de la vieja."""
    _dobles(res, monkeypatch)
    res.precargar(FakePlayable(title="Cómo se llamaba antes"))
    await _esperar_precargas(res)
    tarea = next(iter(res._precargas.values()))
    datos = await res.resolve(FakePlayable(title="Cómo se llama ahora"))
    assert datos["info"]["title"] == datos["userData"][resolver.CLAVE_ORIGEN]["title"] == "Cómo se llama ahora"
    http, _ = tarea.result()
    assert http.raw_data["info"]["title"] == "Unknown title"

@pytest.mark.parametrize("url, esperado", [
    ("https://rr4.googlevideo.com/v?itag=18&expire=1893456000&x=1", 1893456000.0),
    ("https://rr4.googlevideo.com/v?itag=18&x=1", None), ("https://rr4.googlevideo.com/v", None),
    ("https://rr4.googlevideo.com/v?expire=", None), ("https://rr4.googlevideo.com/v?expire=pronto", None),
    ("https://rr4.googlevideo.com/v?expire=nan", None), ("", None),
])
def test_el_vencimiento_sale_del_parametro_expire_si_es_valido(url, esperado):
    assert resolver._vencimiento(url) == esperado

@pytest.mark.parametrize("length, segundos", [(200_000, 200.0), (0, 0.0), (None, 0.0), (-1, 0.0), ("raro", 0.0)])
def test_la_duracion_se_lee_en_segundos_y_nunca_revienta(length, segundos):
    pista = FakePlayable()
    pista.length = length
    assert resolver._duracion_en_s(pista) == segundos
    assert resolver._duracion_en_s(SimpleNamespace()) == 0.0      # sin `length`

async def test_el_tope_descarta_la_precarga_mas_vieja_y_cancela_su_tarea(res, monkeypatch):
    monkeypatch.setattr(resolver, "PRECARGAS_MAXIMAS", 2)
    frenar = asyncio.Event()
    _dobles(res, monkeypatch, frenar=frenar)
    res.precargar(_de_youtube("a"))
    vieja = res._precargas["a"]
    await asyncio.sleep(0)           # `a` ya está dentro de yt-dlp: sigue en curso
    res.precargar(_de_youtube("b"))
    res.precargar(_de_youtube("c"))
    await asyncio.wait([vieja], timeout=1)      # con timeout: si no se cancelara, `a` seguiría frenada
    assert list(res._precargas) == ["b", "c"] and vieja.cancelled()
    frenar.set()
    await _esperar_precargas(res)

async def test_pedir_dos_veces_la_misma_precarga_no_lanza_dos_yt_dlp_y_una_resuelta_se_precarga_por_su_origen(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch)
    pista = FakePlayable()
    res.precargar(pista)
    tarea = res._precargas["abc"]
    res.precargar(pista)
    await _esperar_precargas(res)
    assert pedidas == [pista.uri] and res._precargas["abc"] is tarea and len(res._precargas) == 1
    res._precargas.clear()
    res.precargar(pista_ya_resuelta())
    await _esperar_precargas(res)
    assert pedidas == [pista.uri, "https://www.youtube.com/watch?v=abc"] and list(res._precargas) == ["abc"]

async def test_los_clips_del_dj_las_pistas_sin_pagina_y_los_videos_sin_caso_no_se_precargan(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch)
    sin_caso = FakePlayable()
    res._sin_caso[sin_caso.uri] = "la borraron"
    res.precargar(FakePlayable(source="local", uri="/tmp/dj_abc.wav", identifier="dj_abc"))
    res.precargar(FakePlayable(source="youtube", uri="", identifier="sin-uri"))
    res.precargar(sin_caso)
    assert res._precargas == {}
    await asyncio.sleep(0)
    assert pedidas == []

# --- el contador de fallos ---------------------------------------------------

async def test_los_fallos_pasajeros_seguidos_se_cuentan_y_un_exito_pone_el_contador_en_cero(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR - 1)
    assert res._fallos_seguidos == resolver.FALLOS_PARA_SONDEAR - 1 and res._sondeo is None
    falso.funcionan.add("android")
    assert await res.direct_url("https://youtu.be/abc") == URL_DE_AUDIO
    assert res._fallos_seguidos == 0
    falso.funcionan.clear()
    await _fallar(res, 1)
    assert res._fallos_seguidos == 1 and res._sondeo is None

async def test_una_salida_sin_url_cuenta_como_fallo_pasajero(monkeypatch):
    _falso_proceso(monkeypatch, salida=b"\n")
    res = _resolver_que_rota()
    assert await res.direct_url("https://youtu.be/abc") is None
    assert res._fallos_seguidos == 1

@pytest.mark.parametrize("kwargs, rkwargs, sin_caso", [
    ({"error": ERROR_DEFINITIVO}, {}, True),      # un video bloqueado
    ({"colgar": {"android"}}, {"timeout": 0.01}, False),      # un timeout
])
async def test_ni_un_video_bloqueado_ni_un_timeout_cuentan_contra_el_cliente(monkeypatch, kwargs, rkwargs, sin_caso):
    _yt_dlp(monkeypatch, **kwargs)
    res = _resolver_que_rota(**rkwargs)
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR + 2)
    assert res._fallos_seguidos == 0 and res._sondeo is None
    assert bool(res._sin_caso) is sin_caso

async def test_un_video_bloqueado_en_medio_ni_suma_ni_resetea(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR - 1)
    falso.error = ERROR_DEFINITIVO
    await _fallar(res, 1)
    assert res._fallos_seguidos == resolver.FALLOS_PARA_SONDEAR - 1 and res._sondeo is None
    falso.error = ERROR_PASAJERO
    await _fallar(res, 1)
    assert res._sondeo is not None
    await _terminar_sondeo(res)

async def test_un_fallo_de_una_llamada_que_arranco_con_otro_cliente_no_cuenta(monkeypatch):
    falso = _yt_dlp(monkeypatch, frenar_pedidas=asyncio.Event())
    res = _resolver_que_rota()
    llamada = asyncio.create_task(res.direct_url("https://youtu.be/abc"))
    await _dejar_correr()
    res.cliente = "ios"
    falso.frenar_pedidas.set()
    assert await llamada is None
    assert res._fallos_seguidos == 0

# --- el sondeo: cuándo se lanza ------------------------------------------------

async def test_al_tercer_fallo_pasajero_se_lanza_un_sondeo_sin_esperarlo_y_no_dos(monkeypatch):
    falso = _yt_dlp(monkeypatch, frenar_pruebas=asyncio.Event())
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR - 1)
    assert res._sondeo is None
    await asyncio.wait_for(_fallar(res, 1), timeout=1)      # la resolución que lo dispara no lo espera
    sondeo = res._sondeo
    assert sondeo is not None and not sondeo.done()
    await _fallar(res, 3)
    assert res._sondeo is sondeo
    falso.frenar_pruebas.set()
    await sondeo
    assert falso.pruebas == ["android_vr", "ios", "mweb"]

async def test_entre_un_sondeo_y_el_siguiente_pasa_al_menos_sondeo_cada(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    reloj = Reloj()
    res = _resolver_que_rota(reloj=reloj)
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    primero = res._sondeo
    await primero
    por_pasada = len(falso.pruebas)
    assert por_pasada == len(CLIENTES) - 1
    reloj.t += resolver.SONDEO_CADA - 1
    await _fallar(res, 2)
    assert res._sondeo is primero and len(falso.pruebas) == por_pasada
    reloj.t += 1
    await _fallar(res, 1)
    assert res._sondeo is not primero
    await res._sondeo
    assert len(falso.pruebas) == 2 * por_pasada

# --- el sondeo: qué hace -------------------------------------------------------

async def test_el_sondeo_se_queda_con_el_primer_cliente_que_funciona_sin_actualizar_y_las_resoluciones_lo_usan(monkeypatch, caplog):
    falso = _yt_dlp(monkeypatch, funcionan={"ios", "mweb"})
    res = _resolver_que_rota(cliente="android_vr", autoactualizar=True)
    with caplog.at_level("WARNING", logger="resolver"):
        await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
        await _terminar_sondeo(res)
    assert falso.pruebas == ["android", "ios"]      # se salta el actual
    assert (res.cliente, res.cliente_configurado) == ("ios", "android_vr")
    assert "android_vr" in caplog.text and "ios" in caplog.text and "YTDLP_PLAYER_CLIENT=ios" in caplog.text
    assert falso.actualizaciones == []
    assert await res.direct_url("https://youtu.be/abc") == URL_DE_AUDIO
    assert falso.pedidas[-1] == ("ios", "https://youtu.be/abc") and res._fallos_seguidos == 0

async def test_el_sondeo_pide_lo_mismo_que_direct_url_con_el_cliente_a_probar(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"android_vr"})
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert res.cliente == "android_vr"
    await res.direct_url("https://youtu.be/abc")
    prueba = next(a for a in falso.llamadas if a[-1] == resolver.VIDEO_DE_PRUEBA and "android_vr" in a[a.index("--extractor-args") + 1])
    assert prueba[:-1] == falso.llamadas[-1][:-1] and prueba[0] == "/otro/yt-dlp"
    assert resolver.FORMATO in prueba and "--get-url" in prueba and "--no-playlist" in prueba

async def test_una_prueba_del_sondeo_que_se_pasa_del_timeout_se_mata_y_se_sigue(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"ios"}, colgar={"android_vr"})
    res = _resolver_que_rota(timeout=0.01)
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert falso.pruebas == ["android_vr", "ios"] and res.cliente == "ios"
    assert falso.de("pedir")[resolver.FALLOS_PARA_SONDEAR].matado

async def test_el_sondeo_no_toca_el_contador_ni_la_tabla_de_videos_sin_caso(monkeypatch):
    falso = _yt_dlp(monkeypatch, error_de_prueba=ERROR_DEFINITIVO)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert falso.pruebas == ["android_vr", "ios", "mweb"]
    assert res._fallos_seguidos == resolver.FALLOS_PARA_SONDEAR and res._sin_caso == {} and res.cliente == "android"

async def test_sin_ningun_cliente_que_funcione_ni_actualizacion_automatica_queda_un_error_en_el_log(monkeypatch, caplog):
    falso = _yt_dlp(monkeypatch)
    res = _resolver_que_rota()
    with caplog.at_level("ERROR", logger="resolver"):
        await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
        await _terminar_sondeo(res)
    assert "Ningún cliente de YouTube" in caplog.text and "YTDLP_AUTOACTUALIZAR" in caplog.text
    assert res.cliente == "android" and falso.actualizaciones == []

async def test_sin_ninguno_que_funcione_actualiza_y_vuelve_a_sondear_empezando_por_el_actual(monkeypatch):
    falso = _yt_dlp(monkeypatch, al_actualizar=lambda f: f.funcionan.add("android"))
    res = _resolver_que_rota(autoactualizar=True)
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert falso.actualizaciones == [["/otro/yt-dlp", "-U"]]
    assert falso.pruebas == ["android_vr", "ios", "mweb", "android"]
    orden = [t for t in falso.orden if t in ("pedir", "actualizar", "actualizada")]
    assert orden == ["pedir"] * (resolver.FALLOS_PARA_SONDEAR + 3) + ["actualizar", "actualizada", "pedir"]
    assert res.cliente == "android" and res._fallos_seguidos == 0

async def test_el_resondeo_tras_actualizar_ocurre_una_sola_vez(monkeypatch, caplog):
    falso = _yt_dlp(monkeypatch)
    res = _resolver_que_rota(autoactualizar=True)
    with caplog.at_level("ERROR", logger="resolver"):
        await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
        await _terminar_sondeo(res)
    assert len(falso.actualizaciones) == 1
    assert falso.pruebas == ["android_vr", "ios", "mweb", "android", "android_vr", "ios", "mweb"]
    assert "sigue sin sonar" in caplog.text

async def test_si_la_actualizacion_falla_no_se_vuelve_a_sondear(monkeypatch):
    falso = _yt_dlp(monkeypatch, actualizacion=1)
    res = _resolver_que_rota(autoactualizar=True)
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert len(falso.actualizaciones) == 1 and falso.pruebas == ["android_vr", "ios", "mweb"]

async def test_un_sondeo_que_revienta_no_escapa_como_tarea_no_recuperada(monkeypatch, caplog):
    sin_atender = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, contexto: sin_atender.append(contexto))
    _yt_dlp(monkeypatch)
    res = _resolver_que_rota()

    async def _revienta(**kwargs):
        raise RuntimeError("algo que nadie previó")

    monkeypatch.setattr(res, "_rotar_cliente", _revienta)
    with caplog.at_level("ERROR", logger="resolver"):
        await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
        await _terminar_sondeo(res)
        gc.collect()
        await asyncio.sleep(0)
    assert sin_atender == [] and "Falló el sondeo" in caplog.text

# --- actualizar yt-dlp -----------------------------------------------------------

async def test_el_yt_dlp_del_venv_se_actualiza_con_pip(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    res = resolver.Resolver(binario=os.path.join(os.path.dirname(sys.executable), "yt-dlp"), reloj=Reloj())
    assert await res.actualizar_ytdlp() is True
    assert falso.actualizaciones == [[sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]]

async def test_cualquier_otro_yt_dlp_se_actualiza_a_si_mismo_y_la_comparacion_de_carpetas_no_resuelve_symlinks(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    assert await _resolver_que_rota().actualizar_ytdlp() is True
    assert falso.actualizaciones == [["/otro/yt-dlp", "-U"]]
    monkeypatch.setattr(resolver.sys, "executable", "/proyecto/.venv/bin/python")
    venv = resolver.Resolver(binario="/proyecto/.venv/bin/yt-dlp")
    otro = resolver.Resolver(binario="/usr/local/bin/yt-dlp")
    assert venv._comando_de_actualizacion() == ["/proyecto/.venv/bin/python", "-m", "pip", "install", "-U", "yt-dlp"]
    assert otro._comando_de_actualizacion() == ["/usr/local/bin/yt-dlp", "-U"]

async def test_tras_actualizar_se_vuelve_a_leer_la_version(monkeypatch, caplog, hoy):
    falso = _yt_dlp(monkeypatch, version="2026.07.04")
    res = _resolver_que_rota()
    await res.revisar_ytdlp()
    assert res.estado_ytdlp()["viejo"] is True
    falso.al_actualizar = lambda f: setattr(f, "version", "2026.10.20")
    with caplog.at_level("INFO", logger="resolver"):
        assert await res.actualizar_ytdlp() is True
    assert falso.orden[-2:] == ["actualizada", "version"]
    assert (res.estado_ytdlp()["version"], res.estado_ytdlp()["viejo"]) == ("2026.10.20", False)
    assert "2026.07.04 -> 2026.10.20" in caplog.text

async def test_si_la_actualizacion_sale_con_error_se_devuelve_false_y_queda_en_el_log(monkeypatch, caplog):
    falso = _yt_dlp(monkeypatch, actualizacion=1)
    res = _resolver_que_rota()
    with caplog.at_level("ERROR", logger="resolver"):
        assert await res.actualizar_ytdlp() is False
    assert "No pude actualizar yt-dlp" in caplog.text and "Successfully installed" in caplog.text
    assert falso.versiones_pedidas == 0 and res._ytdlp_libre.is_set()

async def test_si_no_se_puede_lanzar_la_actualizacion_se_devuelve_false(monkeypatch, caplog):
    _no_lanzable(monkeypatch)
    res = _resolver_que_rota()
    with caplog.at_level("ERROR", logger="resolver"):
        assert await res.actualizar_ytdlp() is False
    assert "No pude lanzar la actualización" in caplog.text and res._ytdlp_libre.is_set()

async def test_una_actualizacion_que_se_pasa_del_tiempo_se_corta(monkeypatch, caplog):
    monkeypatch.setattr(resolver, "TIMEOUT_DE_ACTUALIZACION", 0.01)
    falso = _yt_dlp(monkeypatch, frenar_actualizacion=asyncio.Event())
    res = _resolver_que_rota()
    with caplog.at_level("ERROR", logger="resolver"):
        assert await res.actualizar_ytdlp() is False
    assert falso.de("actualizar")[0].matado and "se pasó" in caplog.text and res._ytdlp_libre.is_set()

@pytest.mark.parametrize("actualizacion", [0, 1], ids=["con éxito", "fallida"])
async def test_entre_un_intento_de_actualizar_y_el_siguiente_pasa_actualizar_cada(monkeypatch, actualizacion):
    falso = _yt_dlp(monkeypatch, actualizacion=actualizacion)
    reloj = Reloj()
    res = _resolver_que_rota(reloj=reloj)
    ok = actualizacion == 0
    assert await res.actualizar_ytdlp() is ok
    reloj.t += resolver.ACTUALIZAR_CADA - 1
    assert await res.actualizar_ytdlp() is False
    assert len(falso.actualizaciones) == 1
    reloj.t += 1
    assert await res.actualizar_ytdlp() is ok
    assert len(falso.actualizaciones) == 2

async def test_dos_actualizaciones_a_la_vez_lanzan_un_solo_comando(monkeypatch):
    falso = _yt_dlp(monkeypatch, frenar_actualizacion=asyncio.Event())
    res = _resolver_que_rota()
    primera = asyncio.create_task(res.actualizar_ytdlp())
    await _dejar_correr()
    assert await res.actualizar_ytdlp() is False
    falso.frenar_actualizacion.set()
    assert await primera is True
    assert len(falso.actualizaciones) == 1

async def test_mientras_se_actualiza_direct_url_espera_a_que_termine(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"android"}, frenar_actualizacion=asyncio.Event())
    res = _resolver_que_rota()
    actualizando = asyncio.create_task(res.actualizar_ytdlp())
    await _dejar_correr()
    assert len(falso.actualizaciones) == 1
    pidiendo = asyncio.create_task(res.direct_url("https://youtu.be/abc"))
    await _dejar_correr()
    assert not pidiendo.done() and falso.pedidas == []
    falso.frenar_actualizacion.set()
    assert await actualizando is True
    assert await pidiendo == URL_DE_AUDIO
    assert falso.orden.index("actualizada") < falso.orden.index("pedir")

async def test_la_actualizacion_espera_a_los_yt_dlp_en_curso(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"android"}, frenar_pedidas=asyncio.Event())
    res = _resolver_que_rota()
    pidiendo = asyncio.create_task(res.direct_url("https://youtu.be/abc"))
    await _dejar_correr()
    actualizando = asyncio.create_task(res.actualizar_ytdlp())
    await _dejar_correr()
    assert falso.actualizaciones == []
    falso.frenar_pedidas.set()
    assert await pidiendo == URL_DE_AUDIO
    assert await actualizando is True
    assert len(falso.actualizaciones) == 1

async def test_el_sondeo_tambien_espera_a_que_termine_la_actualizacion(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"ios"}, frenar_actualizacion=asyncio.Event())
    res = _resolver_que_rota()
    actualizando = asyncio.create_task(res.actualizar_ytdlp())
    await _dejar_correr()
    res._sondeo = asyncio.create_task(res._sondear())
    await _dejar_correr()
    assert falso.pruebas == []
    falso.frenar_actualizacion.set()
    await actualizando
    await _terminar_sondeo(res)
    assert res.cliente == "ios"

async def test_fuera_de_una_actualizacion_las_resoluciones_corren_en_paralelo(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"android"}, frenar_pedidas=asyncio.Event())
    res = _resolver_que_rota()
    uno = asyncio.create_task(res.direct_url("https://youtu.be/uno"))
    otro = asyncio.create_task(res.direct_url("https://youtu.be/otro"))
    await _dejar_correr()
    assert len(falso.pedidas) == 2
    falso.frenar_pedidas.set()
    assert await uno == URL_DE_AUDIO and await otro == URL_DE_AUDIO

async def test_cancelar_una_actualizacion_mata_al_comando_y_deja_libre_a_yt_dlp(monkeypatch):
    falso = _yt_dlp(monkeypatch, frenar_actualizacion=asyncio.Event())
    res = _resolver_que_rota()
    tarea = asyncio.create_task(res.actualizar_ytdlp())
    await _dejar_correr()
    assert not res._ytdlp_libre.is_set()
    await _cancelar(tarea)
    assert falso.de("actualizar")[0].matado and res._ytdlp_libre.is_set()

# --- mantener_al_dia -----------------------------------------------------------

async def _mantener(res, segundos=0.0):
    tarea = asyncio.create_task(res.mantener_al_dia())
    await _dejar_correr()
    if segundos:
        await asyncio.sleep(segundos)
    await _cancelar(tarea)
    return tarea

@pytest.mark.parametrize("version, autoactualizar, actualizaciones, final, viejo", [
    ("2026.07.04", True, 1, "2026.10.20", False),
    ("2026.07.04", False, 0, "2026.07.04", True),
    ("2026.10.20", True, 0, "2026.10.20", False),
])
async def test_solo_se_actualiza_si_esta_vieja_y_la_opcion_esta_activa(monkeypatch, hoy, version, autoactualizar, actualizaciones, final, viejo):
    falso = _yt_dlp(monkeypatch, version=version, al_actualizar=lambda f: setattr(f, "version", "2026.10.20"))
    res = _resolver_que_rota(autoactualizar=autoactualizar)
    await _mantener(res)
    assert len(falso.actualizaciones) == actualizaciones
    assert (res.estado_ytdlp()["version"], res.estado_ytdlp()["viejo"]) == (final, viejo)

async def test_mantener_al_dia_repite_la_revision_cada_revisar_cada_y_se_cancela_limpio(monkeypatch, hoy):
    monkeypatch.setattr(resolver, "REVISAR_CADA", 0.01)
    falso = _yt_dlp(monkeypatch, version="2026.10.20")
    tarea = await _mantener(_resolver_que_rota(), segundos=0.2)
    assert falso.versiones_pedidas >= 3 and tarea.cancelled()

async def test_si_la_actualizacion_no_arregla_la_version_no_se_reintenta_en_bucle(monkeypatch, hoy):
    monkeypatch.setattr(resolver, "REVISAR_CADA", 0.01)
    falso = _yt_dlp(monkeypatch, version="2026.07.04")
    await _mantener(_resolver_que_rota(autoactualizar=True), segundos=0.2)
    assert falso.versiones_pedidas >= 3 and len(falso.actualizaciones) == 1

async def test_mantener_al_dia_cancelada_en_plena_actualizacion_no_deja_huerfano_a_pip(monkeypatch, hoy):
    falso = _yt_dlp(monkeypatch, version="2026.07.04", frenar_actualizacion=asyncio.Event())
    res = _resolver_que_rota(autoactualizar=True)
    tarea = asyncio.create_task(res.mantener_al_dia())
    await _dejar_correr()
    assert not res._ytdlp_libre.is_set()
    await _cancelar(tarea)
    assert falso.de("actualizar")[0].matado and res._ytdlp_libre.is_set()

async def test_un_tropiezo_en_la_revision_no_mata_la_tarea(monkeypatch, caplog):
    monkeypatch.setattr(resolver, "REVISAR_CADA", 0.01)
    res = _resolver_que_rota()
    llamadas = []

    async def _revisar():
        llamadas.append(1)
        if len(llamadas) == 1:
            raise RuntimeError("algo que nadie previó")
        return {}

    monkeypatch.setattr(res, "revisar_ytdlp", _revisar)
    with caplog.at_level("ERROR", logger="resolver"):
        await _mantener(res, segundos=0.1)
    assert len(llamadas) >= 2 and "Falló la revisión de yt-dlp" in caplog.text

# --- SponsorBlock: las partes que no son música ----------------------------------

async def test_la_intro_que_no_es_musica_se_salta_arrancando_en_su_final(monkeypatch, caplog):
    vc, recibidos = _player_con(monkeypatch, SBQueAnota({"abc": [INTRO, DEL_MEDIO]}))
    with caplog.at_level("INFO", logger="resolver"):
        await vc.play(FakePlayable())
    assert recibidos == [{"start": 12_500}] and vc.arranque_ms == 12_500
    assert "Salto la intro" in caplog.text and "Un tema" in caplog.text

@pytest.mark.parametrize("reanudar, start, esperado", [
    ({"abc": 60_000}, None, 60_000),      # un start mayor gana a la intro
    ({}, 45_000, 45_000),
    ({}, 5_000, 12_500),      # uno menor cede
])
async def test_el_start_se_compara_con_el_final_de_la_intro(monkeypatch, reanudar, start, esperado):
    vc, recibidos = _player_con(monkeypatch, SBQueAnota({"abc": [INTRO]}), reanudar=reanudar)
    await vc.play(FakePlayable(), **({} if start is None else {"start": start}))
    assert recibidos == [{"start": esperado}] and vc.arranque_ms == esperado

@pytest.mark.parametrize("segmentos", [[DEL_MEDIO], [Segmento(0, 199_000, 200_000)]], ids=["sin intro", "intro que se come la canción"])
async def test_sin_una_intro_que_saltar_el_start_no_se_toca(monkeypatch, segmentos):
    vc, recibidos = _player_con(monkeypatch, SBQueAnota({"abc": segmentos}))
    await vc.play(FakePlayable())
    await vc.play(FakePlayable(identifier="otro", uri="https://www.youtube.com/watch?v=otro"))
    assert recibidos == [{}, {}] and vc.arranque_ms == 0

async def test_la_consulta_de_sponsorblock_corre_junto_a_yt_dlp(monkeypatch):
    """Si esperara a que yt-dlp terminara, esta resolución no terminaría nunca."""
    sb = SBQueAnota({"abc": [INTRO]})
    vc, recibidos = _player_con(monkeypatch, sb)

    async def _resolve_que_espera_la_consulta(pista):
        while not sb.preguntados:
            await asyncio.sleep(0)
        return pista

    monkeypatch.setattr(vc.resolver, "resolve", _resolve_que_espera_la_consulta)
    await asyncio.wait_for(vc.play(FakePlayable()), timeout=2)
    assert recibidos == [{"start": 12_500}]

async def test_las_pistas_que_no_son_de_youtube_no_preguntan_nada(monkeypatch):
    sb = SBQueAnota({"abc": [INTRO]})
    vc, recibidos = _player_con(monkeypatch, sb)
    await vc.play(FakePlayable(source="local", uri="file:///dj_cache/corte.wav", identifier="corte"))
    assert sb.preguntados == [] and recibidos == [{}]

async def test_si_sponsorblock_no_responde_la_cancion_suena_entera(monkeypatch, caplog):
    sb = SponsorBlock(activo=True, url="http://127.0.0.1:9/api/skipSegments", timeout=0.3)
    vc, recibidos = _player_con(monkeypatch, sb)
    with caplog.at_level("WARNING", logger="sponsorblock"):
        assert await vc.play(FakePlayable()) is not None
    await sb.cerrar()
    assert recibidos == [{}] and "SponsorBlock no respondió" in caplog.text

async def test_con_sponsorblock_apagado_no_se_consulta_nada(monkeypatch):
    sb = SponsorBlock()
    assert sb.activo is False
    vc, recibidos = _player_con(monkeypatch, sb)
    pista = FakePlayable()
    _dobles(vc.resolver, monkeypatch)
    await vc.play(pista)
    vc.resolver.precargar(pista)
    await _esperar_precargas(vc.resolver)
    assert recibidos == [{}] and sb._http is None and sb._en_curso == {}
    assert await vc.resolver.intro_de(pista) == 0 and vc.resolver.segmentos_de(pista) == ()

async def test_precargar_tambien_pide_los_segmentos_de_la_que_viene(monkeypatch):
    sb = SBQueAnota({"abc": [INTRO, DEL_MEDIO]})
    res = resolver.Resolver(sponsorblock=sb)
    _dobles(res, monkeypatch)
    pista = FakePlayable()
    res.precargar(pista)
    res.precargar(pista)
    await _esperar_precargas(res)
    await asyncio.sleep(0)
    assert sb.preguntados == ["abc"] and res.segmentos_de(pista) == (INTRO, DEL_MEDIO)

async def test_precargar_no_pide_segmentos_de_un_clip_ni_de_un_video_sin_caso(monkeypatch):
    sb = SBQueAnota()
    res = resolver.Resolver(sponsorblock=sb)
    _dobles(res, monkeypatch)
    bloqueado = _de_youtube("bloqueado")
    res._anotar_sin_caso(bloqueado.uri, "está bloqueada")
    res.precargar(FakePlayable(source="local", uri="file:///dj_cache/corte.wav", identifier="corte"))
    res.precargar(bloqueado)
    await asyncio.sleep(0)
    assert sb.preguntados == []

def test_segmentos_de_lee_solo_la_cache_y_descarta_los_de_otra_edicion():
    sb = SBQueAnota()
    res = resolver.Resolver(sponsorblock=sb)
    sb._cache["abc"] = (INTRO, Segmento(50_000, 60_000, 300_000))
    assert res.segmentos_de(FakePlayable()) == (INTRO,)
    assert res.segmentos_de(_de_youtube("sin_cache")) == ()
    assert res.segmentos_de(FakePlayable(source="local", uri="file:///x.wav", identifier="abc")) == ()
    assert sb.preguntados == []

def test_el_video_de_una_pista_sale_del_origen_si_ya_estaba_resuelta():
    assert resolver.video_de(FakePlayable()) == resolver.video_de(pista_ya_resuelta()) == "abc"
    assert resolver.video_de(pista_http()) == resolver.video_de(FakePlayable(source="local", uri="file:///x.wav")) == ""

async def test_cerrar_el_resolver_cierra_la_sesion_de_sponsorblock():
    sb = SBQueAnota()
    cerrado = []

    async def _cerrar():
        cerrado.append(True)

    sb.cerrar = _cerrar
    await resolver.Resolver(sponsorblock=sb).cerrar()
    assert cerrado == [True]

def test_cada_resolver_tiene_su_sponsorblock_y_lo_toma_de_la_configuracion(monkeypatch):
    monkeypatch.setattr(resolver.config, "SPONSORBLOCK", True)
    uno, otro = resolver.Resolver(), resolver.Resolver()
    assert uno.sponsorblock is not otro.sponsorblock and uno.sponsorblock.activo is True
