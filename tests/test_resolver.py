"""El resolver: yt-dlp saca la URL, Lavalink la reproduce.

Lo que se protege aquí es que la pista que termina sonando se siga *viendo* como
la de YouTube (el panel y el DJ leen esos datos) y que ningún tropiezo de yt-dlp
tumbe la reproducción: ante la duda, se devuelve la pista original.
"""

import asyncio
import gc
import os
import sys
import time
from datetime import date
from types import SimpleNamespace

import pytest

from core import resolver
from core.sponsorblock import Segmento, SponsorBlock


@pytest.fixture
def res():
    """Un `Resolver` nuevo por test: nada de lo que anota llega al siguiente."""
    return resolver.Resolver()


class FakePlayable:
    """Lo mínimo que resolver.py le pide a un wavelink.Playable."""

    def __init__(self, source="youtube", uri="https://www.youtube.com/watch?v=abc",
                 title="Un tema", author="Alguien", identifier="abc"):
        self.source = source
        self.uri = uri
        self.title = title
        self.identifier = identifier
        self.length = 200_000
        self.raw_data = {
            "encoded": f"encoded-{identifier}",
            "info": {
                "title": title,
                "author": author,
                "identifier": identifier,
                "uri": uri,
                "artworkUrl": "https://img/portada.jpg",
                "length": 200_000,
                "isSeekable": True,
                "isStream": False,
                "position": 0,
                "sourceName": source,
            },
            "pluginInfo": {},
            "userData": {},
        }

    @property
    def extras(self):
        """Como en wavelink: `userData` se lee de vuelta por aquí."""
        return self.raw_data["userData"]


def pista_http():
    p = FakePlayable(
        source="http", uri="https://rr4.googlevideo.com/videoplayback?x=1",
        title="Unknown title", author="Unknown artist", identifier="rr4-loquesea",
    )
    p.raw_data["encoded"] = "encoded-http"
    return p


# --- la cara de la pista -----------------------------------------------------

def test_la_pista_que_suena_conserva_el_encoded_del_stream(monkeypatch):
    hecha = _fusionar(monkeypatch)
    # El `encoded` es lo que Lavalink usa para reproducir: tiene que ser el http.
    assert hecha["encoded"] == "encoded-http"


def test_la_pista_que_suena_muestra_los_datos_de_youtube(monkeypatch):
    info = _fusionar(monkeypatch)["info"]
    assert info["title"] == "Un tema"
    assert info["author"] == "Alguien"
    assert info["artworkUrl"] == "https://img/portada.jpg"
    # El identifier es la clave de los reintentos del cog: tiene que ser el de
    # YouTube, no el de la URL de googlevideo (que cambia en cada resolución).
    assert info["identifier"] == "abc"


def test_el_encoded_sigue_siendo_el_del_stream_http(monkeypatch):
    """Lo que importa de la fuente es qué reproduce Lavalink, y eso no cambia.

    Este test reemplaza a uno que exigía `sourceName == "http"`. Esa decisión se
    dio vuelta a propósito (ver el test del autoplay más abajo): el nombre de la
    fuente es un dato nuestro que nunca sale del proceso, mientras que el
    `encoded` sí es lo que Lavalink va a tocar.
    """
    hecha = _fusionar(monkeypatch)
    assert hecha["encoded"] == "encoded-http"


def _fusionar(monkeypatch):
    """Corre la fusión de metadatos sin construir un wavelink.Playable real."""
    capturado = {}
    monkeypatch.setattr(
        resolver.wavelink, "Playable", lambda datos: capturado.setdefault("d", datos)
    )
    resolver._con_la_cara_del_original(pista_http(), FakePlayable())
    return capturado["d"]


# --- resolve -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_lo_que_no_es_de_youtube_pasa_derecho(res, monkeypatch):
    async def _no_llamar(url):
        raise AssertionError("no había que resolver nada")
    monkeypatch.setattr(res, "direct_url", _no_llamar)

    clip = FakePlayable(source="local", uri="/tmp/dj_abc.wav")
    assert await res.resolve(clip) is clip


@pytest.mark.asyncio
async def test_si_yt_dlp_no_puede_se_devuelve_la_original(res, monkeypatch):
    async def _sin_url(url):
        return None
    monkeypatch.setattr(res, "direct_url", _sin_url)

    pista = FakePlayable()
    assert await res.resolve(pista) is pista


@pytest.mark.asyncio
async def test_si_lavalink_no_carga_la_url_se_devuelve_la_original(res, monkeypatch):
    async def _url(url):
        return "https://rr4.googlevideo.com/videoplayback?x=1"
    async def _vacio(identifier):
        return []
    monkeypatch.setattr(res, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _vacio)
    monkeypatch.setattr(resolver, "PAUSA_ENTRE_CARGAS", 0)

    pista = FakePlayable()
    assert await res.resolve(pista) is pista


@pytest.mark.asyncio
async def test_si_lavalink_falla_una_vez_se_reintenta_con_la_misma_url(res, monkeypatch):
    """Un tropiezo suelto de Lavalink con la URL no debe mandar al plugin."""
    monkeypatch.setattr(resolver, "PAUSA_ENTRE_CARGAS", 0)
    pedidas = []
    async def _url(url):
        return "https://rr4.googlevideo.com/videoplayback?x=1"
    async def _fetch(identifier):
        pedidas.append(identifier)
        if len(pedidas) == 1:
            raise RuntimeError("Read timed out")
        return [pista_http()]
    monkeypatch.setattr(res, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _fetch)
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: datos)

    pista = FakePlayable()
    assert await res.resolve(pista) is not pista
    assert pedidas == ["https://rr4.googlevideo.com/videoplayback?x=1"] * 2


@pytest.mark.asyncio
async def test_si_lavalink_sigue_fallando_se_rinde_tras_los_intentos(res, monkeypatch):
    monkeypatch.setattr(resolver, "PAUSA_ENTRE_CARGAS", 0)
    pedidas = []
    async def _url(url):
        return "https://rr4.googlevideo.com/videoplayback?x=1"
    async def _fetch(identifier):
        pedidas.append(identifier)
        raise RuntimeError("Read timed out")
    monkeypatch.setattr(res, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _fetch)

    pista = FakePlayable()
    assert await res.resolve(pista) is pista
    assert len(pedidas) == resolver.INTENTOS_DE_CARGA


# --- direct_url --------------------------------------------------------------

@pytest.mark.asyncio
async def test_direct_url_se_queda_con_la_linea_de_la_url(res, monkeypatch):
    _falso_proceso(monkeypatch, salida=b"algo de ruido\nhttps://rr4.googlevideo.com/v?x=1\n")
    assert await res.direct_url("https://youtu.be/abc") == "https://rr4.googlevideo.com/v?x=1"


@pytest.mark.asyncio
async def test_direct_url_devuelve_none_si_yt_dlp_falla(res, monkeypatch):
    _falso_proceso(monkeypatch, salida=b"", error=b"ERROR: Sign in to confirm", codigo=1)
    assert await res.direct_url("https://youtu.be/abc") is None


@pytest.mark.asyncio
async def test_direct_url_sin_url_no_llama_a_nada(res, monkeypatch):
    def _no_llamar(*a, **kw):
        raise AssertionError("no había que lanzar yt-dlp")
    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _no_llamar)
    assert await res.direct_url("") is None


@pytest.mark.asyncio
async def test_el_texto_va_como_argumento_nunca_por_shell(res, monkeypatch):
    """Las URLs vienen de YouTube: si pasaran por una shell serían inyectables."""
    argumentos = _falso_proceso(monkeypatch, salida=b"https://rr4.googlevideo.com/v\n")
    await res.direct_url("https://youtu.be/abc; rm -rf /")
    assert argumentos["args"][-1] == "https://youtu.be/abc; rm -rf /"
    assert argumentos["args"][0] == resolver.config.YTDLP_BIN


async def test_cancelar_direct_url_mata_a_yt_dlp(res, monkeypatch):
    """Una precarga descartada se cancela a mitad: yt-dlp no puede quedar huérfano."""
    import asyncio

    matado = []
    lanzado = asyncio.Event()

    class ProcesoLento:
        returncode = None

        async def communicate(self):
            lanzado.set()
            await asyncio.sleep(3600)

        def kill(self):
            matado.append(True)

    async def _exec(*args, **kwargs):
        return ProcesoLento()

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)
    tarea = asyncio.create_task(res.direct_url("https://youtu.be/abc"))
    await lanzado.wait()
    tarea.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert matado == [True]


def _falso_proceso(monkeypatch, salida=b"", error=b"", codigo=0):
    capturado = {}

    class FakeProc:
        returncode = codigo

        async def communicate(self):
            return salida, error

        def kill(self):
            pass

    async def _exec(*args, **kwargs):
        capturado["args"] = list(args)
        return FakeProc()

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)
    return capturado


# --- el origen viaja en userData ---------------------------------------------

def test_la_pista_que_suena_lleva_el_origen_de_youtube_en_userdata(monkeypatch):
    """Lo único que sobrevive el viaje a Lavalink y vuelta es `userData`.

    Los campos de `info` que pisamos viven solo en este proceso: `play()` le
    manda a Lavalink el `encoded` y el `userData`, nada más. Sin esto, cuando la
    pista vuelve en un evento de excepción ya no hay forma de saber de qué video
    de YouTube salía.
    """
    origen = _fusionar(monkeypatch)["userData"]["yt_origen"]
    assert origen["uri"] == "https://www.youtube.com/watch?v=abc"
    assert origen["identifier"] == "abc"
    assert origen["title"] == "Un tema"
    assert origen["author"] == "Alguien"
    assert origen["artworkUrl"] == "https://img/portada.jpg"


def pista_ya_resuelta():
    """La pista http tal como vuelve de Lavalink en un evento de excepción.

    Llega con la cara de la fuente http —la uri de googlevideo ya caducada y
    "Unknown title"— y lo único que la ata a YouTube es el `userData`.
    """
    p = pista_http()
    p.raw_data["userData"] = {
        resolver.CLAVE_ORIGEN: {
            "uri": "https://www.youtube.com/watch?v=abc",
            "identifier": "abc",
            "title": "Un tema",
            "author": "Alguien",
            "artworkUrl": "https://img/portada.jpg",
        }
    }
    return p


@pytest.mark.asyncio
async def test_el_reintento_pide_una_url_nueva_con_la_uri_de_youtube(res, monkeypatch):
    """El bug que se está arreglando: sin esto se repetía la URL ya muerta."""
    pedidas = []

    async def _url(url):
        pedidas.append(url)
        return "https://rr9.googlevideo.com/videoplayback?fresca=1"

    async def _encontrar(identifier):
        return [pista_http()]

    monkeypatch.setattr(res, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _encontrar)
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: datos)

    await res.resolve(pista_ya_resuelta())

    assert pedidas == ["https://www.youtube.com/watch?v=abc"]


@pytest.mark.asyncio
async def test_la_pista_reintentada_no_pierde_la_cara(res, monkeypatch):
    """En el reintento no se le puede copiar la cara a partir de sí misma.

    La pista que vuelve de Lavalink ya está degradada ("Unknown title", uri de
    googlevideo), así que los datos que se ven salen del origen guardado. Si no,
    a la segunda vuelta el panel mostraría "Unknown title" para siempre.
    """
    async def _url(url):
        return "https://rr9.googlevideo.com/videoplayback?fresca=1"

    async def _encontrar(identifier):
        return [pista_http()]

    monkeypatch.setattr(res, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _encontrar)
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: datos)

    datos = await res.resolve(pista_ya_resuelta())

    assert datos["info"]["title"] == "Un tema"
    assert datos["info"]["identifier"] == "abc"
    # Y el origen se vuelve a llevar: tiene que aguantar todas las vueltas que dé.
    assert datos["userData"][resolver.CLAVE_ORIGEN]["uri"] == "https://www.youtube.com/watch?v=abc"


# --- fallos que no tienen arreglo --------------------------------------------

@pytest.mark.asyncio
async def test_un_video_bloqueado_queda_marcado_como_definitivo(res, monkeypatch):
    """Reintentar un video bloqueado no va a servir nunca: hay que saltearlo ya."""
    _falso_proceso(
        monkeypatch, salida=b"", codigo=1,
        error=b"ERROR: [youtube] abc: It was blocked due to the claimed content by Paramount Global (PMN).",
    )

    assert await res.direct_url("https://www.youtube.com/watch?v=abc") is None
    assert res.motivo_definitivo(FakePlayable()) == "está bloqueada"


@pytest.mark.asyncio
async def test_un_tropiezo_pasajero_no_se_marca(res, monkeypatch):
    """Un 400 o un timeout sí mejoran reintentando: no hay que quemarlos."""
    _falso_proceso(
        monkeypatch, salida=b"", codigo=1,
        error=b"ERROR: unable to download video data: HTTP Error 400: Bad Request",
    )

    await res.direct_url("https://www.youtube.com/watch?v=abc")
    assert res.motivo_definitivo(FakePlayable()) is None


@pytest.mark.asyncio
async def test_el_motivo_se_reconoce_por_el_origen_guardado(res, monkeypatch):
    """En el reintento la pista llega con la uri de googlevideo, no la de YouTube."""
    _falso_proceso(
        monkeypatch, salida=b"", codigo=1,
        error=b"ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
    )

    await res.direct_url("https://www.youtube.com/watch?v=abc")
    assert res.motivo_definitivo(pista_ya_resuelta()) == "la borraron"


# --- yt-dlp viejo -------------------------------------------------------------

def test_la_version_de_yt_dlp_dice_cuantos_dias_tiene():
    """Es el punto único de falla de toda la música y su versión es una fecha."""
    from datetime import date
    assert resolver.antiguedad_de("2026.07.04", hoy=date(2026, 8, 28)) == 55


def test_una_version_que_no_es_fecha_no_rompe_nada():
    from datetime import date
    assert resolver.antiguedad_de("2026.07.04.dev0+g1a2b3c", hoy=date(2026, 8, 28)) == 55
    assert resolver.antiguedad_de("vaya uno a saber", hoy=date(2026, 8, 28)) is None
    assert resolver.antiguedad_de("", hoy=date(2026, 8, 28)) is None


@pytest.mark.asyncio
async def test_revisar_ytdlp_deja_el_estado_a_mano_del_panel(res, monkeypatch):
    _falso_proceso(monkeypatch, salida=b"2026.07.04\n")
    from datetime import date
    monkeypatch.setattr(resolver, "_hoy", lambda: date(2026, 8, 28))

    await res.revisar_ytdlp()

    estado = res.estado_ytdlp()
    assert (estado["version"], estado["dias"], estado["viejo"]) == ("2026.07.04", 55, True)


def test_la_pista_resuelta_se_presenta_como_de_youtube(monkeypatch):
    """El autoplay de wavelink siembra recomendaciones con esto.

    `_do_recommendation()` arma su lista con `[t.identifier for t in seeds if
    t.source == "youtube"]`. Diciendo "http" no hay semilla, no hay query, y el
    player se queda sin de dónde sacar la próxima canción.

    Decir "youtube" es seguro: a Lavalink solo le viaja el `encoded`, que sigue
    siendo el del stream http, y la uri que acompaña es la de YouTube, que es
    justo con la que hay que volver a resolver.
    """
    info = _fusionar(monkeypatch)["info"]
    assert info["sourceName"] == "youtube"
    assert info["identifier"] == "abc"


async def test_el_player_avisa_que_esta_resolviendo(res, monkeypatch):
    """Mientras yt-dlp trabaja, `playing` da falso: `resolviendo` es lo que
    impide que el cog largue otro tema encima."""
    import wavelink

    vc = resolver.ResolvingPlayer.__new__(resolver.ResolvingPlayer)
    vc.resolver = res
    vc.reanudar = {}
    vc.resolviendo = 0
    visto = []

    async def _resolve(pista):
        visto.append(vc.resolviendo)
        return pista

    async def _play(self, pista, *a, **kw):
        visto.append(vc.resolviendo)
        return pista

    monkeypatch.setattr(res, "resolve", _resolve)
    monkeypatch.setattr(wavelink.Player, "play", _play)
    monkeypatch.setattr(wavelink.Player, "connected", property(lambda self: True))

    await vc.play(FakePlayable())

    assert visto == [1, 1]
    assert vc.resolviendo == 0


# --- yt-dlp que no se puede lanzar ---------------------------------------------

@pytest.mark.parametrize("error", [FileNotFoundError("no existe"), PermissionError("sin permiso")])
async def test_direct_url_con_el_binario_roto_devuelve_none(res, monkeypatch, error):
    """Un OSError al lanzar yt-dlp no puede escapar: en el autoplay mataría la cola."""
    async def _exec(*args, **kwargs):
        raise error

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)
    assert await res.direct_url("https://youtu.be/abc") is None


async def test_resolve_cae_a_la_pista_original_si_yt_dlp_no_se_puede_lanzar(res, monkeypatch):
    async def _exec(*args, **kwargs):
        raise FileNotFoundError("no existe")

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)
    pista = FakePlayable()
    assert await res.resolve(pista) is pista


# --- play() después de desconectarse ---------------------------------------------

def _player_que_resuelve(monkeypatch, conectado):
    import wavelink

    vc = resolver.ResolvingPlayer.__new__(resolver.ResolvingPlayer)
    vc.resolver = resolver.Resolver()
    vc.reanudar = {}
    vc.resolviendo = 0
    llamadas = []

    async def _resolve(pista):
        return pista

    async def _play(self, pista, *a, **kw):
        llamadas.append(pista)
        return pista

    monkeypatch.setattr(vc.resolver, "resolve", _resolve)
    monkeypatch.setattr(wavelink.Player, "play", _play)
    monkeypatch.setattr(wavelink.Player, "connected", property(lambda self: conectado))
    return vc, llamadas


async def test_play_no_toca_lavalink_si_el_player_se_desconecto_mientras_resolvia(monkeypatch):
    """Si no, el PATCH recrearía un player huérfano de un servidor ya cerrado."""
    vc, llamadas = _player_que_resuelve(monkeypatch, conectado=False)

    assert await vc.play(FakePlayable()) is None

    assert llamadas == []
    assert vc.resolviendo == 0


async def test_play_sigue_tocando_si_el_player_esta_conectado(monkeypatch):
    vc, llamadas = _player_que_resuelve(monkeypatch, conectado=True)
    pista = FakePlayable()

    assert await vc.play(pista) is pista

    assert llamadas == [pista]


# --- el estado vive en la instancia -----------------------------------------------

def _extractor_de(argumentos):
    """Lo que fue detrás de `--extractor-args` en la última llamada a yt-dlp."""
    args = argumentos["args"]
    return args[args.index("--extractor-args") + 1]


def test_sin_argumentos_el_resolver_toma_la_configuracion_al_construirse(monkeypatch):
    monkeypatch.setattr(resolver.config, "YTDLP_BIN", "/otro/yt-dlp")
    monkeypatch.setattr(resolver.config, "YTDLP_TIMEOUT", 7)
    monkeypatch.setattr(resolver.config, "YTDLP_PLAYER_CLIENT", "mweb")

    res = resolver.Resolver()

    assert (res.binario, res.timeout, res.cliente) == ("/otro/yt-dlp", 7, "mweb")
    # Se lee al construir, no en cada llamada: tocar la configuración después no
    # mueve a un resolver que ya existe (para eso está `res.cliente`).
    monkeypatch.setattr(resolver.config, "YTDLP_PLAYER_CLIENT", "ios")
    assert res.cliente == "mweb"


def test_los_argumentos_del_constructor_ganan_a_la_configuracion():
    res = resolver.Resolver(binario="/opt/yt-dlp", cliente="ios", timeout=5)
    assert (res.binario, res.timeout, res.cliente) == ("/opt/yt-dlp", 5, "ios")


async def test_el_cliente_se_puede_cambiar_en_caliente(monkeypatch):
    """Rotar de cliente cuando el actual falla no puede exigir reiniciar el bot.

    El argumento se arma en cada llamada: si se calculara al importar (como
    antes) o al construir, cambiar `cliente` no tendría ningún efecto.
    """
    res = resolver.Resolver(cliente="android")
    argumentos = _falso_proceso(monkeypatch, salida=b"https://rr4.googlevideo.com/v\n")

    await res.direct_url("https://youtu.be/abc")
    assert _extractor_de(argumentos) == "youtube:player_client=android"

    res.cliente = "ios"
    await res.direct_url("https://youtu.be/abc")
    assert _extractor_de(argumentos) == "youtube:player_client=ios"


async def test_el_binario_del_constructor_es_el_que_se_lanza(monkeypatch):
    res = resolver.Resolver(binario="/opt/yt-dlp/yt-dlp")

    argumentos = _falso_proceso(monkeypatch, salida=b"https://rr4.googlevideo.com/v\n")
    await res.direct_url("https://youtu.be/abc")
    assert argumentos["args"][0] == "/opt/yt-dlp/yt-dlp"

    argumentos = _falso_proceso(monkeypatch, salida=b"2026.07.04\n")
    await res.revisar_ytdlp()
    assert argumentos["args"][0] == "/opt/yt-dlp/yt-dlp"


class _ProcesoLento:
    """Un yt-dlp que tarda `demora` segundos en contestar."""

    returncode = 0

    def __init__(self, demora):
        self.demora = demora
        self.matado = False

    async def communicate(self):
        await asyncio.sleep(self.demora)
        return b"https://rr4.googlevideo.com/v\n", b""

    def kill(self):
        self.matado = True

    async def wait(self):
        return 0


def _lanzar_proceso(monkeypatch, proceso):
    async def _exec(*args, **kwargs):
        return proceso
    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)


async def test_el_timeout_del_constructor_corta_a_yt_dlp(monkeypatch):
    proceso = _ProcesoLento(demora=10)
    _lanzar_proceso(monkeypatch, proceso)

    res = resolver.Resolver(timeout=0.05)

    assert await res.direct_url("https://youtu.be/abc") is None
    assert proceso.matado


async def test_con_mas_timeout_yt_dlp_alcanza_a_contestar(monkeypatch):
    proceso = _ProcesoLento(demora=0.05)
    _lanzar_proceso(monkeypatch, proceso)

    res = resolver.Resolver(timeout=5)

    assert await res.direct_url("https://youtu.be/abc") == "https://rr4.googlevideo.com/v"
    assert not proceso.matado


async def test_dos_resolvers_no_comparten_los_videos_sin_caso(monkeypatch):
    uno, otro = resolver.Resolver(), resolver.Resolver()
    _falso_proceso(
        monkeypatch, salida=b"", codigo=1,
        error=b"ERROR: [youtube] abc: It was blocked due to the claimed content by Paramount Global (PMN).",
    )

    await uno.direct_url("https://www.youtube.com/watch?v=abc")

    assert uno.motivo_definitivo(FakePlayable()) == "está bloqueada"
    assert otro.motivo_definitivo(FakePlayable()) is None


async def test_olvidar_motivos_vacia_la_tabla_de_ese_resolver(res, monkeypatch):
    _falso_proceso(
        monkeypatch, salida=b"", codigo=1,
        error=b"ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
    )
    await res.direct_url("https://www.youtube.com/watch?v=abc")
    assert res.motivo_definitivo(FakePlayable()) == "es privada"

    res.olvidar_motivos()

    assert res.motivo_definitivo(FakePlayable()) is None


async def test_la_tabla_de_videos_sin_caso_esta_acotada(res, monkeypatch):
    """Es una ayuda para no quemar reintentos, no un registro histórico."""
    monkeypatch.setattr(resolver, "RECUERDO_MAXIMO", 2)
    _falso_proceso(
        monkeypatch, salida=b"", codigo=1,
        error=b"ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader",
    )

    for letra in "abc":
        await res.direct_url(f"https://www.youtube.com/watch?v={letra}")

    def visto(letra):
        return res.motivo_definitivo(
            FakePlayable(uri=f"https://www.youtube.com/watch?v={letra}")
        )

    assert visto("a") is None       # la más vieja se cayó
    assert visto("b") == "la borraron"
    assert visto("c") == "la borraron"


async def test_dos_resolvers_no_comparten_el_estado_de_yt_dlp(monkeypatch):
    from datetime import date
    uno, otro = resolver.Resolver(), resolver.Resolver()
    _falso_proceso(monkeypatch, salida=b"2026.07.04\n")
    monkeypatch.setattr(resolver, "_hoy", lambda: date(2026, 8, 28))

    await uno.revisar_ytdlp()

    assert uno.estado_ytdlp()["version"] == "2026.07.04"
    assert uno.estado_ytdlp()["viejo"] is True
    assert otro.estado_ytdlp()["version"] is None
    assert otro.estado_ytdlp()["viejo"] is False


def test_el_estado_de_yt_dlp_se_entrega_como_copia(res):
    """Quien lo lee (la API) no puede pisarle el estado al resolver."""
    res.estado_ytdlp()["version"] = "tocada"
    assert res.estado_ytdlp()["version"] is None


def _nodo_de_mentira():
    """Lo mínimo del nodo que `wavelink.Player.__init__` lee al construirse."""
    return SimpleNamespace(
        players={}, client=None,
        _inactive_channel_tokens=None, _inactive_player_timeout=None,
    )


def test_el_player_toma_el_resolver_del_bot():
    del_bot = resolver.Resolver()

    vc = resolver.ResolvingPlayer(
        SimpleNamespace(resolver=del_bot), "canal-de-voz", nodes=[_nodo_de_mentira()]
    )

    assert vc.resolver is del_bot


def test_el_player_arma_el_suyo_si_el_bot_no_tiene():
    uno = resolver.ResolvingPlayer(SimpleNamespace(), "canal-de-voz", nodes=[_nodo_de_mentira()])
    otro = resolver.ResolvingPlayer(SimpleNamespace(), "canal-de-voz", nodes=[_nodo_de_mentira()])

    assert isinstance(uno.resolver, resolver.Resolver)
    assert uno.resolver is not otro.resolver


# --- precargar la que viene ------------------------------------------------------

def _url_que_vence_en(segundos):
    """Una URL de googlevideo con `expire`, como las que entrega yt-dlp."""
    return f"https://rr4.googlevideo.com/videoplayback?itag=18&expire={int(time.time() + segundos)}&x=1"


def _de_youtube(id_):
    """Una pista de YouTube distinta por cada `id_`, para llenar la tabla."""
    return FakePlayable(
        uri=f"https://www.youtube.com/watch?v={id_}", title=f"Tema {id_}", identifier=id_
    )


def _dobles(res, monkeypatch, url=None, frenar=None):
    """yt-dlp y Lavalink de mentira. Devuelve la lista de páginas que se le pidieron.

    Con `frenar` (un `asyncio.Event`), yt-dlp se queda esperando hasta que alguien
    lo suelte: así una precarga puede quedar en curso mientras pasa otra cosa.
    """
    pedidas = []

    async def _url(pagina):
        pedidas.append(pagina)
        if frenar is not None:
            await frenar.wait()
        return url or _url_que_vence_en(6 * 3600)

    async def _encontrar(identifier):
        return [pista_http()]

    monkeypatch.setattr(res, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _encontrar)
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: datos)
    return pedidas


async def _esperar_precargas(res):
    """Deja terminar las precargas sin recoger su resultado ni su excepción."""
    tareas = list(res._precargas.values())
    if tareas:
        await asyncio.wait(tareas)


async def test_resolve_usa_la_url_precargada_sin_volver_a_pedirla(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch)
    pista = FakePlayable()

    res.precargar(pista)
    await _esperar_precargas(res)
    datos = await res.resolve(pista)

    assert pedidas == [pista.uri]
    assert datos["encoded"] == "encoded-http"
    assert datos["info"]["title"] == "Un tema"
    assert datos["userData"][resolver.CLAVE_ORIGEN]["identifier"] == "abc"


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

    assert pedidas == [pista.uri]
    assert datos["encoded"] == "encoded-http"


async def test_una_precarga_vencida_se_descarta_y_se_resuelve_de_nuevo(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch, url=_url_que_vence_en(-60))
    pista = FakePlayable()

    res.precargar(pista)
    await _esperar_precargas(res)
    await res.resolve(pista)

    assert pedidas == [pista.uri, pista.uri]


async def test_la_precarga_tiene_que_cubrir_la_duracion_y_el_margen(res, monkeypatch):
    """No basta con que la URL siga viva al arrancar: tiene que durar todo el tema."""
    pista = FakePlayable()      # dura 200 s
    duracion = pista.length / 1000

    justa = _dobles(
        res, monkeypatch,
        url=_url_que_vence_en(duracion + resolver.MARGEN_DE_VENCIMIENTO - 30),
    )
    res.precargar(pista)
    await _esperar_precargas(res)
    await res.resolve(pista)
    assert len(justa) == 2        # le faltaban 30 s de margen: se pidió otra

    sobrada = _dobles(
        res, monkeypatch,
        url=_url_que_vence_en(duracion + resolver.MARGEN_DE_VENCIMIENTO + 60),
    )
    res.precargar(pista)
    await _esperar_precargas(res)
    await res.resolve(pista)
    assert len(sobrada) == 1      # con un minuto de sobra, alcanza


async def test_sin_duracion_conocida_basta_con_el_margen(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch, url=_url_que_vence_en(resolver.MARGEN_DE_VENCIMIENTO + 60))
    pista = FakePlayable()
    pista.length = 0

    res.precargar(pista)
    await _esperar_precargas(res)
    await res.resolve(pista)

    assert len(pedidas) == 1


async def test_sin_expire_se_supone_una_vida_corta(res, monkeypatch):
    """Sin `expire` la URL vive `VIDA_SIN_EXPIRE`: sirve para un tema corto, no para uno largo."""
    sin_expire = "https://rr4.googlevideo.com/videoplayback?itag=18&x=1"
    pedidas = _dobles(res, monkeypatch, url=sin_expire)

    corta = FakePlayable()
    res.precargar(corta)
    await _esperar_precargas(res)
    await res.resolve(corta)
    assert len(pedidas) == 1

    larga = FakePlayable()
    larga.length = (resolver.VIDA_SIN_EXPIRE + 60) * 1000
    res.precargar(larga)
    await _esperar_precargas(res)
    await res.resolve(larga)
    assert len(pedidas) == 3      # la precarga no alcanzó: una de más


async def test_si_yt_dlp_no_pudo_precargar_se_resuelve_por_el_camino_normal(res, monkeypatch):
    respuestas = [None, _url_que_vence_en(6 * 3600)]
    pedidas = []

    async def _url(pagina):
        pedidas.append(pagina)
        return respuestas.pop(0)

    async def _encontrar(identifier):
        return [pista_http()]

    monkeypatch.setattr(res, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _encontrar)
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: datos)
    pista = FakePlayable()

    res.precargar(pista)
    await _esperar_precargas(res)
    datos = await res.resolve(pista)

    assert len(pedidas) == 2
    assert datos["encoded"] == "encoded-http"


async def test_si_lavalink_no_pudo_precargar_se_resuelve_por_el_camino_normal(res, monkeypatch):
    monkeypatch.setattr(resolver, "PAUSA_ENTRE_CARGAS", 0)
    pedidas = _dobles(res, monkeypatch)
    intentos = []

    async def _fetch(identifier):
        intentos.append(identifier)
        if len(intentos) <= resolver.INTENTOS_DE_CARGA:
            raise RuntimeError("Read timed out")      # se rinde toda la precarga
        return [pista_http()]

    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _fetch)
    pista = FakePlayable()

    res.precargar(pista)
    await _esperar_precargas(res)
    datos = await res.resolve(pista)

    assert len(pedidas) == 2
    assert datos["encoded"] == "encoded-http"


async def test_una_precarga_que_revienta_no_escapa_como_tarea_no_recuperada(res, monkeypatch, caplog):
    """Nadie mira la tarea hasta que le toca sonar, y a lo mejor nunca."""
    sin_atender = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, contexto: sin_atender.append(contexto))
    pedidas = _dobles(res, monkeypatch)
    original = res._cargar
    llamadas = []

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
        # Se suelta la tarea sin consumirla: es el caso que antes se perdía.
        res._precargas.clear()
        gc.collect()
        await asyncio.sleep(0)

    assert sin_atender == []
    assert "Falló la precarga" in caplog.text

    # Y la pista suena igual, por el camino normal.
    res.precargar(pista)
    await _esperar_precargas(res)
    datos = await res.resolve(pista)
    assert datos["encoded"] == "encoded-http"
    assert len(pedidas) == 1


async def test_cada_precarga_se_usa_una_sola_vez(res, monkeypatch):
    """En un reintento tras un fallo no puede volver la misma URL."""
    pedidas = _dobles(res, monkeypatch)
    pista = FakePlayable()

    res.precargar(pista)
    await _esperar_precargas(res)
    await res.resolve(pista)
    assert pedidas == [pista.uri]
    assert res._precargas == {}

    await res.resolve(pista)

    assert pedidas == [pista.uri, pista.uri]


async def test_la_precarga_se_muestra_con_la_cara_de_la_pista_actual(res, monkeypatch):
    """Entre precargar y sonar la pista pudo cambiar: la cara no sale de la vieja."""
    _dobles(res, monkeypatch)
    res.precargar(FakePlayable(title="Cómo se llamaba antes"))
    await _esperar_precargas(res)

    datos = await res.resolve(FakePlayable(title="Cómo se llama ahora"))

    assert datos["info"]["title"] == "Cómo se llama ahora"
    assert datos["userData"][resolver.CLAVE_ORIGEN]["title"] == "Cómo se llama ahora"


async def test_la_precarga_no_se_contamina_con_la_cara_de_otro_uso(res, monkeypatch):
    """`_con_la_cara_del_original` copia: la pista guardada no queda pisada."""
    _dobles(res, monkeypatch)
    pista = FakePlayable()
    res.precargar(pista)
    await _esperar_precargas(res)
    tarea = next(iter(res._precargas.values()))

    await res.resolve(pista)

    http, _ = tarea.result()
    assert http.raw_data["info"]["title"] == "Unknown title"


def test_el_vencimiento_sale_del_parametro_expire():
    assert resolver._vencimiento("https://rr4.googlevideo.com/v?itag=18&expire=1893456000&x=1") == 1893456000.0


@pytest.mark.parametrize("url", [
    "https://rr4.googlevideo.com/v?itag=18&x=1",
    "https://rr4.googlevideo.com/v",
    "https://rr4.googlevideo.com/v?expire=",
    "https://rr4.googlevideo.com/v?expire=pronto",
    "https://rr4.googlevideo.com/v?expire=nan",
    "",
])
def test_sin_un_expire_valido_no_hay_vencimiento(url):
    assert resolver._vencimiento(url) is None


@pytest.mark.parametrize("length, segundos", [
    (200_000, 200.0), (0, 0.0), (None, 0.0), (-1, 0.0), ("raro", 0.0),
])
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
    # Con timeout: si no se cancelara, `a` seguiría frenada y esto colgaría el test.
    await asyncio.wait([vieja], timeout=1)

    assert list(res._precargas) == ["b", "c"]
    assert vieja.cancelled()

    frenar.set()
    await _esperar_precargas(res)


async def test_pedir_dos_veces_la_misma_precarga_no_lanza_dos_yt_dlp(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch)
    pista = FakePlayable()

    res.precargar(pista)
    tarea = res._precargas["abc"]
    res.precargar(pista)
    await _esperar_precargas(res)

    assert pedidas == [pista.uri]
    assert res._precargas["abc"] is tarea
    assert len(res._precargas) == 1


async def test_los_clips_del_dj_y_las_pistas_sin_pagina_no_se_precargan(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch)

    res.precargar(FakePlayable(source="local", uri="/tmp/dj_abc.wav", identifier="dj_abc"))
    res.precargar(FakePlayable(source="youtube", uri="", identifier="sin-uri"))

    assert res._precargas == {}
    await asyncio.sleep(0)
    assert pedidas == []


async def test_una_pista_ya_resuelta_se_precarga_por_su_origen_guardado(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch)

    res.precargar(pista_ya_resuelta())
    await _esperar_precargas(res)

    assert pedidas == ["https://www.youtube.com/watch?v=abc"]
    assert list(res._precargas) == ["abc"]


async def test_un_video_que_ya_se_sabe_sin_caso_no_se_precarga(res, monkeypatch):
    pedidas = _dobles(res, monkeypatch)
    pista = FakePlayable()
    res._sin_caso[pista.uri] = "la borraron"

    res.precargar(pista)

    assert res._precargas == {}
    await asyncio.sleep(0)
    assert pedidas == []


async def test_resolve_sin_precarga_sigue_siendo_igual(res, monkeypatch):
    """La precarga es opcional: sin ella, una sola pasada por yt-dlp como siempre."""
    pedidas = _dobles(res, monkeypatch)
    pista = FakePlayable()

    datos = await res.resolve(pista)

    assert pedidas == [pista.uri]
    assert datos["encoded"] == "encoded-http"
    assert res._precargas == {}


# --- yt-dlp que se cuida solo: rotar de cliente y actualizarse -----------------------

ERROR_PASAJERO = b"ERROR: unable to download video data: HTTP Error 400: Bad Request"
ERROR_DEFINITIVO = b"ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader"
URL_DE_AUDIO = "https://rr4.googlevideo.com/v"
CLIENTES = ("android", "android_vr", "ios", "mweb")


class Reloj:
    """La hora monotónica de mentira: solo avanza si el test la mueve."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Proceso:
    """Un proceso hijo de mentira; con `frenar`, no contesta hasta que lo suelten."""

    def __init__(self, salida=b"", error=b"", codigo=0, frenar=None, al_terminar=None):
        self.salida, self.error, self.returncode = salida, error, codigo
        self.frenar, self.al_terminar = frenar, al_terminar
        self.matado = False

    async def communicate(self):
        if self.frenar is not None:
            await self.frenar.wait()
        if self.al_terminar is not None:
            self.al_terminar()
        return self.salida, self.error

    def kill(self):
        self.matado = True

    async def wait(self):
        return self.returncode


class YtDlpFalso:
    """Un `create_subprocess_exec` que contesta según lo que le pidan.

    `--get-url` da una URL si el cliente está en `funcionan` y un error pasajero si no;
    `--version` da `version`; cualquier otra cosa es la actualización.
    """

    def __init__(self, funcionan=(), *, version="2026.10.01", actualizacion=0,
                 error=ERROR_PASAJERO, error_de_prueba=None, colgar=(),
                 frenar_pruebas=None, frenar_pedidas=None, frenar_actualizacion=None,
                 al_actualizar=None):
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
                salida=b"Successfully installed yt-dlp", codigo=self.actualizacion,
                frenar=self.frenar_actualizacion,
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


# --- el contador de fallos ----------------------------------------------------------

async def test_los_fallos_pasajeros_seguidos_se_cuentan(monkeypatch):
    _yt_dlp(monkeypatch)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR - 1)
    assert res._fallos_seguidos == resolver.FALLOS_PARA_SONDEAR - 1
    assert res._sondeo is None


async def test_una_salida_sin_url_cuenta_como_fallo_pasajero(monkeypatch):
    _falso_proceso(monkeypatch, salida=b"\n", codigo=0)
    res = _resolver_que_rota()
    assert await res.direct_url("https://youtu.be/abc") is None
    assert res._fallos_seguidos == 1


async def test_los_videos_bloqueados_no_cuentan_contra_el_cliente(monkeypatch):
    _yt_dlp(monkeypatch, error=ERROR_DEFINITIVO)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR + 2)
    assert res._fallos_seguidos == 0
    assert res._sondeo is None
    assert res._sin_caso


async def test_un_timeout_no_cuenta_contra_el_cliente(monkeypatch):
    _yt_dlp(monkeypatch, colgar={"android"})
    res = _resolver_que_rota(timeout=0.01)
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR + 2)
    assert res._fallos_seguidos == 0
    assert res._sondeo is None


async def test_un_yt_dlp_que_no_se_puede_lanzar_no_cuenta_contra_el_cliente(monkeypatch):
    async def _exec(*args, **kwargs):
        raise FileNotFoundError("no existe")

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR + 2)
    assert res._fallos_seguidos == 0
    assert res._sondeo is None


async def test_un_exito_pone_el_contador_en_cero(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR - 1)
    falso.funcionan.add("android")
    assert await res.direct_url("https://youtu.be/abc") == URL_DE_AUDIO
    assert res._fallos_seguidos == 0
    falso.funcionan.clear()
    await _fallar(res, 1)
    assert res._fallos_seguidos == 1
    assert res._sondeo is None


async def test_un_video_bloqueado_en_medio_ni_suma_ni_resetea(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR - 1)
    falso.error = ERROR_DEFINITIVO
    await _fallar(res, 1)
    assert res._fallos_seguidos == resolver.FALLOS_PARA_SONDEAR - 1
    assert res._sondeo is None
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


# --- el sondeo: cuándo se lanza ------------------------------------------------------

async def test_al_tercer_fallo_pasajero_se_lanza_un_sondeo_y_no_dos(monkeypatch):
    falso = _yt_dlp(monkeypatch, frenar_pruebas=asyncio.Event())
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR - 1)
    assert res._sondeo is None
    await _fallar(res, 1)
    sondeo = res._sondeo
    assert sondeo is not None and not sondeo.done()
    await _fallar(res, 3)
    assert res._sondeo is sondeo
    falso.frenar_pruebas.set()
    await sondeo
    assert falso.pruebas == ["android_vr", "ios", "mweb"]


async def test_la_resolucion_que_dispara_el_sondeo_no_lo_espera(monkeypatch):
    falso = _yt_dlp(monkeypatch, frenar_pruebas=asyncio.Event())
    res = _resolver_que_rota()
    await asyncio.wait_for(_fallar(res, resolver.FALLOS_PARA_SONDEAR), timeout=1)
    assert not res._sondeo.done()
    falso.frenar_pruebas.set()
    await res._sondeo


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
    assert res._sondeo is primero
    assert len(falso.pruebas) == por_pasada
    reloj.t += 1
    await _fallar(res, 1)
    assert res._sondeo is not primero
    await res._sondeo
    assert len(falso.pruebas) == 2 * por_pasada


# --- el sondeo: qué hace --------------------------------------------------------------

async def test_el_sondeo_se_queda_con_el_primer_cliente_que_funciona_y_se_salta_el_actual(monkeypatch, caplog):
    falso = _yt_dlp(monkeypatch, funcionan={"ios", "mweb"})
    res = _resolver_que_rota(cliente="android_vr")
    with caplog.at_level("WARNING", logger="resolver"):
        await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
        await _terminar_sondeo(res)
    assert falso.pruebas == ["android", "ios"]
    assert res.cliente == "ios"
    assert res.cliente_configurado == "android_vr"
    assert "android_vr" in caplog.text and "ios" in caplog.text
    assert "YTDLP_PLAYER_CLIENT=ios" in caplog.text


async def test_tras_rotar_las_resoluciones_usan_el_cliente_nuevo(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"ios"})
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert await res.direct_url("https://youtu.be/abc") == URL_DE_AUDIO
    assert falso.pedidas[-1] == ("ios", "https://youtu.be/abc")
    assert res._fallos_seguidos == 0


async def test_el_sondeo_pide_lo_mismo_que_direct_url_con_el_cliente_a_probar(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"android_vr"})
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert res.cliente == "android_vr"
    await res.direct_url("https://youtu.be/abc")
    prueba = next(a for a in falso.llamadas if a[-1] == resolver.VIDEO_DE_PRUEBA and "android_vr" in a[a.index("--extractor-args") + 1])
    assert prueba[:-1] == falso.llamadas[-1][:-1]
    assert prueba[0] == "/otro/yt-dlp"
    assert resolver.FORMATO in prueba and "--get-url" in prueba and "--no-playlist" in prueba


async def test_una_prueba_del_sondeo_que_se_pasa_del_timeout_se_mata_y_se_sigue(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"ios"}, colgar={"android_vr"})
    res = _resolver_que_rota(timeout=0.01)
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert falso.pruebas == ["android_vr", "ios"]
    assert res.cliente == "ios"
    assert falso.de("pedir")[resolver.FALLOS_PARA_SONDEAR].matado


async def test_el_sondeo_no_toca_el_contador_ni_la_tabla_de_videos_sin_caso(monkeypatch):
    falso = _yt_dlp(monkeypatch, error_de_prueba=ERROR_DEFINITIVO)
    res = _resolver_que_rota()
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert falso.pruebas == ["android_vr", "ios", "mweb"]
    assert res._fallos_seguidos == resolver.FALLOS_PARA_SONDEAR
    assert res._sin_caso == {}
    assert res.cliente == "android"


async def test_sin_ningun_cliente_que_funcione_queda_un_error_en_el_log(monkeypatch, caplog):
    _yt_dlp(monkeypatch)
    res = _resolver_que_rota()
    with caplog.at_level("ERROR", logger="resolver"):
        await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
        await _terminar_sondeo(res)
    assert "Ningún cliente de YouTube" in caplog.text
    assert res.cliente == "android"


async def test_sin_actualizacion_automatica_el_sondeo_no_actualiza_nada(monkeypatch, caplog):
    falso = _yt_dlp(monkeypatch)
    res = _resolver_que_rota(autoactualizar=False)
    with caplog.at_level("ERROR", logger="resolver"):
        await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
        await _terminar_sondeo(res)
    assert falso.actualizaciones == []
    assert "YTDLP_AUTOACTUALIZAR" in caplog.text


async def test_si_un_cliente_sirve_no_se_actualiza_nada(monkeypatch):
    falso = _yt_dlp(monkeypatch, funcionan={"mweb"})
    res = _resolver_que_rota(autoactualizar=True)
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert res.cliente == "mweb"
    assert falso.actualizaciones == []


async def test_sin_ninguno_que_funcione_actualiza_y_vuelve_a_sondear_empezando_por_el_actual(monkeypatch):
    falso = _yt_dlp(monkeypatch, al_actualizar=lambda f: f.funcionan.add("android"))
    res = _resolver_que_rota(autoactualizar=True)
    await _fallar(res, resolver.FALLOS_PARA_SONDEAR)
    await _terminar_sondeo(res)
    assert falso.actualizaciones == [["/otro/yt-dlp", "-U"]]
    assert falso.pruebas == ["android_vr", "ios", "mweb", "android"]
    orden = [t for t in falso.orden if t in ("pedir", "actualizar", "actualizada")]
    assert orden == ["pedir"] * (resolver.FALLOS_PARA_SONDEAR + 3) + ["actualizar", "actualizada", "pedir"]
    assert res.cliente == "android"
    assert res._fallos_seguidos == 0


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
    assert len(falso.actualizaciones) == 1
    assert falso.pruebas == ["android_vr", "ios", "mweb"]


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
    assert sin_atender == []
    assert "Falló el sondeo" in caplog.text


# --- actualizar yt-dlp -----------------------------------------------------------------

async def test_el_yt_dlp_del_venv_se_actualiza_con_pip(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    res = resolver.Resolver(binario=os.path.join(os.path.dirname(sys.executable), "yt-dlp"), reloj=Reloj())
    assert await res.actualizar_ytdlp() is True
    assert falso.actualizaciones == [[sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]]


def test_la_comparacion_de_carpetas_no_resuelve_symlinks(monkeypatch):
    monkeypatch.setattr(resolver.sys, "executable", "/proyecto/.venv/bin/python")
    venv = resolver.Resolver(binario="/proyecto/.venv/bin/yt-dlp")
    otro = resolver.Resolver(binario="/usr/local/bin/yt-dlp")
    assert venv._comando_de_actualizacion() == ["/proyecto/.venv/bin/python", "-m", "pip", "install", "-U", "yt-dlp"]
    assert otro._comando_de_actualizacion() == ["/usr/local/bin/yt-dlp", "-U"]


async def test_cualquier_otro_yt_dlp_se_actualiza_a_si_mismo(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    assert await _resolver_que_rota().actualizar_ytdlp() is True
    assert falso.actualizaciones == [["/otro/yt-dlp", "-U"]]


async def test_tras_actualizar_se_vuelve_a_leer_la_version(monkeypatch, caplog):
    monkeypatch.setattr(resolver, "_hoy", lambda: date(2026, 10, 22))
    falso = _yt_dlp(monkeypatch, version="2026.07.04")
    res = _resolver_que_rota()
    await res.revisar_ytdlp()
    assert res.estado_ytdlp()["viejo"] is True
    falso.al_actualizar = lambda f: setattr(f, "version", "2026.10.20")
    with caplog.at_level("INFO", logger="resolver"):
        assert await res.actualizar_ytdlp() is True
    assert falso.orden[-2:] == ["actualizada", "version"]
    assert res.estado_ytdlp()["version"] == "2026.10.20"
    assert res.estado_ytdlp()["viejo"] is False
    assert "2026.07.04 -> 2026.10.20" in caplog.text


async def test_si_la_actualizacion_sale_con_error_se_devuelve_false_y_queda_en_el_log(monkeypatch, caplog):
    falso = _yt_dlp(monkeypatch, actualizacion=1)
    res = _resolver_que_rota()
    with caplog.at_level("ERROR", logger="resolver"):
        assert await res.actualizar_ytdlp() is False
    assert "No pude actualizar yt-dlp" in caplog.text
    assert "Successfully installed" in caplog.text
    assert falso.versiones_pedidas == 0
    assert res._ytdlp_libre.is_set()


async def test_si_no_se_puede_lanzar_la_actualizacion_se_devuelve_false(monkeypatch, caplog):
    async def _exec(*args, **kwargs):
        raise FileNotFoundError("no existe")

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)
    res = _resolver_que_rota()
    with caplog.at_level("ERROR", logger="resolver"):
        assert await res.actualizar_ytdlp() is False
    assert "No pude lanzar la actualización" in caplog.text
    assert res._ytdlp_libre.is_set()


async def test_una_actualizacion_que_se_pasa_del_tiempo_se_corta(monkeypatch, caplog):
    monkeypatch.setattr(resolver, "TIMEOUT_DE_ACTUALIZACION", 0.01)
    falso = _yt_dlp(monkeypatch, frenar_actualizacion=asyncio.Event())
    res = _resolver_que_rota()
    with caplog.at_level("ERROR", logger="resolver"):
        assert await res.actualizar_ytdlp() is False
    assert falso.de("actualizar")[0].matado
    assert "se pasó" in caplog.text
    assert res._ytdlp_libre.is_set()


async def test_entre_un_intento_de_actualizar_y_el_siguiente_pasa_actualizar_cada(monkeypatch):
    falso = _yt_dlp(monkeypatch)
    reloj = Reloj()
    res = _resolver_que_rota(reloj=reloj)
    assert await res.actualizar_ytdlp() is True
    reloj.t += resolver.ACTUALIZAR_CADA - 1
    assert await res.actualizar_ytdlp() is False
    assert len(falso.actualizaciones) == 1
    reloj.t += 1
    assert await res.actualizar_ytdlp() is True
    assert len(falso.actualizaciones) == 2


async def test_un_intento_fallido_tambien_espera_actualizar_cada(monkeypatch):
    falso = _yt_dlp(monkeypatch, actualizacion=1)
    res = _resolver_que_rota()
    assert await res.actualizar_ytdlp() is False
    assert await res.actualizar_ytdlp() is False
    assert len(falso.actualizaciones) == 1


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
    assert not pidiendo.done()
    assert falso.pedidas == []
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
    assert falso.de("actualizar")[0].matado
    assert res._ytdlp_libre.is_set()


# --- mantener_al_dia ---------------------------------------------------------------------

@pytest.fixture
def hoy(monkeypatch):
    """Día fijo: `2026.07.04` es viejo (110 días) y `2026.10.20` no."""
    monkeypatch.setattr(resolver, "_hoy", lambda: date(2026, 10, 22))


async def _mantener(res, segundos=0.0):
    tarea = asyncio.create_task(res.mantener_al_dia())
    await _dejar_correr()
    if segundos:
        await asyncio.sleep(segundos)
    await _cancelar(tarea)


async def test_si_esta_vieja_y_la_opcion_esta_activa_se_actualiza(monkeypatch, hoy):
    falso = _yt_dlp(monkeypatch, version="2026.07.04", al_actualizar=lambda f: setattr(f, "version", "2026.10.20"))
    res = _resolver_que_rota(autoactualizar=True)
    await _mantener(res)
    assert len(falso.actualizaciones) == 1
    assert (res.estado_ytdlp()["version"], res.estado_ytdlp()["viejo"]) == ("2026.10.20", False)


@pytest.mark.parametrize("version, autoactualizar", [("2026.07.04", False), ("2026.10.20", True)])
async def test_no_se_actualiza_si_no_esta_vieja_o_la_opcion_esta_apagada(monkeypatch, hoy, version, autoactualizar):
    falso = _yt_dlp(monkeypatch, version=version)
    res = _resolver_que_rota(autoactualizar=autoactualizar)
    await _mantener(res)
    assert falso.actualizaciones == []
    assert res.estado_ytdlp()["version"] == version


async def test_mantener_al_dia_repite_la_revision_cada_revisar_cada(monkeypatch, hoy):
    monkeypatch.setattr(resolver, "REVISAR_CADA", 0.01)
    falso = _yt_dlp(monkeypatch, version="2026.10.20")
    await _mantener(_resolver_que_rota(), segundos=0.2)
    assert falso.versiones_pedidas >= 3


async def test_si_la_actualizacion_no_arregla_la_version_no_se_reintenta_en_bucle(monkeypatch, hoy):
    monkeypatch.setattr(resolver, "REVISAR_CADA", 0.01)
    falso = _yt_dlp(monkeypatch, version="2026.07.04")
    await _mantener(_resolver_que_rota(autoactualizar=True), segundos=0.2)
    assert falso.versiones_pedidas >= 3
    assert len(falso.actualizaciones) == 1


async def test_mantener_al_dia_se_cancela_limpio_mientras_duerme(monkeypatch, hoy):
    _yt_dlp(monkeypatch, version="2026.10.20")
    tarea = asyncio.create_task(_resolver_que_rota().mantener_al_dia())
    await _dejar_correr()
    await _cancelar(tarea)
    assert tarea.cancelled()


async def test_mantener_al_dia_cancelada_en_plena_actualizacion_no_deja_huerfano_a_pip(monkeypatch, hoy):
    falso = _yt_dlp(monkeypatch, version="2026.07.04", frenar_actualizacion=asyncio.Event())
    res = _resolver_que_rota(autoactualizar=True)
    tarea = asyncio.create_task(res.mantener_al_dia())
    await _dejar_correr()
    assert not res._ytdlp_libre.is_set()
    await _cancelar(tarea)
    assert falso.de("actualizar")[0].matado
    assert res._ytdlp_libre.is_set()


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
    assert len(llamadas) >= 2
    assert "Falló la revisión de yt-dlp" in caplog.text


# --- el estado que ven los paneles -----------------------------------------------------------

def test_el_estado_de_yt_dlp_trae_el_cliente_actual_y_el_configurado():
    res = resolver.Resolver(cliente="android")
    assert res.estado_ytdlp() == {
        "version": None, "dias": None, "viejo": False,
        "cliente": "android", "cliente_configurado": "android",
    }
    res.cliente = "android_vr"
    assert res.estado_ytdlp()["cliente"] == "android_vr"
    assert res.estado_ytdlp()["cliente_configurado"] == "android"


async def test_el_estado_de_yt_dlp_conserva_lo_de_siempre(monkeypatch, hoy):
    _yt_dlp(monkeypatch, version="2026.07.04")
    res = _resolver_que_rota()
    await res.revisar_ytdlp()
    estado = res.estado_ytdlp()
    assert (estado["version"], estado["dias"], estado["viejo"]) == ("2026.07.04", 110, True)


def test_sin_argumentos_el_resolver_toma_los_clientes_y_la_actualizacion_de_la_configuracion(monkeypatch):
    monkeypatch.setattr(resolver.config, "YTDLP_CLIENTES", ("ios", "web"))
    monkeypatch.setattr(resolver.config, "YTDLP_AUTOACTUALIZAR", False)
    res = resolver.Resolver()
    assert res.clientes == ("ios", "web")
    assert res.autoactualizar is False
    monkeypatch.setattr(resolver.config, "YTDLP_AUTOACTUALIZAR", True)
    assert res.autoactualizar is False


def test_los_argumentos_del_constructor_ganan_para_los_clientes_y_la_actualizacion(monkeypatch):
    monkeypatch.setattr(resolver.config, "YTDLP_AUTOACTUALIZAR", False)
    res = resolver.Resolver(clientes=["web"], autoactualizar=True)
    assert res.clientes == ("web",)
    assert res.autoactualizar is True


# --- SponsorBlock: las partes que no son música ----------------------------------------

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


def _player_con(monkeypatch, sb, reanudar=None):
    """Un `ResolvingPlayer` con ese SponsorBlock; devuelve los kwargs con que llega a `Player.play`."""
    import wavelink

    vc = resolver.ResolvingPlayer.__new__(resolver.ResolvingPlayer)
    vc.resolver = resolver.Resolver(sponsorblock=sb)
    vc.reanudar = reanudar or {}
    vc.resolviendo = 0
    vc.arranque_ms = 0
    recibidos = []

    async def _resolve(pista):
        return pista

    async def _play(self, pista, *a, **kw):
        recibidos.append(kw)
        return pista

    monkeypatch.setattr(vc.resolver, "resolve", _resolve)
    monkeypatch.setattr(wavelink.Player, "play", _play)
    monkeypatch.setattr(wavelink.Player, "connected", property(lambda self: True))
    return vc, recibidos


INTRO = Segmento(0, 12_500, 200_000)
DEL_MEDIO = Segmento(90_000, 100_000, 200_000)


async def test_la_intro_que_no_es_musica_se_salta_arrancando_en_su_final(monkeypatch, caplog):
    vc, recibidos = _player_con(monkeypatch, SBQueAnota({"abc": [INTRO, DEL_MEDIO]}))

    with caplog.at_level("INFO", logger="resolver"):
        await vc.play(FakePlayable())

    assert recibidos == [{"start": 12_500}]
    assert vc.arranque_ms == 12_500
    assert "Salto la intro" in caplog.text and "Un tema" in caplog.text


async def test_un_start_mayor_gana_a_la_intro(monkeypatch):
    vc, recibidos = _player_con(monkeypatch, SBQueAnota({"abc": [INTRO]}), reanudar={"abc": 60_000})

    await vc.play(FakePlayable())
    await vc.play(FakePlayable(), start=45_000)

    assert [r["start"] for r in recibidos] == [60_000, 45_000]
    assert vc.arranque_ms == 45_000


async def test_un_start_menor_que_la_intro_cede(monkeypatch):
    vc, recibidos = _player_con(monkeypatch, SBQueAnota({"abc": [INTRO]}))

    await vc.play(FakePlayable(), start=5_000)

    assert recibidos == [{"start": 12_500}]


async def test_sin_intro_el_start_no_se_toca(monkeypatch):
    vc, recibidos = _player_con(monkeypatch, SBQueAnota({"abc": [DEL_MEDIO]}))

    await vc.play(FakePlayable())
    await vc.play(FakePlayable(identifier="otro", uri="https://www.youtube.com/watch?v=otro"))

    assert recibidos == [{}, {}]
    assert vc.arranque_ms == 0


async def test_una_intro_que_se_come_la_cancion_entera_no_se_salta(monkeypatch):
    sb = SBQueAnota({"abc": [Segmento(0, 199_000, 200_000)]})
    vc, recibidos = _player_con(monkeypatch, sb)

    await vc.play(FakePlayable())

    assert recibidos == [{}]


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

    assert recibidos == [{}]
    assert "SponsorBlock no respondió" in caplog.text


async def test_con_sponsorblock_apagado_no_se_consulta_nada(monkeypatch):
    sb = SponsorBlock()
    assert sb.activo is False
    vc, recibidos = _player_con(monkeypatch, sb)
    pista = FakePlayable()
    _dobles(vc.resolver, monkeypatch)

    await vc.play(pista)
    vc.resolver.precargar(pista)
    await _esperar_precargas(vc.resolver)

    assert recibidos == [{}]
    assert sb._http is None and sb._en_curso == {}
    assert await vc.resolver.intro_de(pista) == 0
    assert vc.resolver.segmentos_de(pista) == ()


async def test_precargar_tambien_pide_los_segmentos_de_la_que_viene(monkeypatch):
    sb = SBQueAnota({"abc": [INTRO, DEL_MEDIO]})
    res = resolver.Resolver(sponsorblock=sb)
    _dobles(res, monkeypatch)
    pista = FakePlayable()

    res.precargar(pista)
    res.precargar(pista)
    await _esperar_precargas(res)
    await asyncio.sleep(0)

    assert sb.preguntados == ["abc"]
    assert res.segmentos_de(pista) == (INTRO, DEL_MEDIO)


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
    otra_edicion = Segmento(50_000, 60_000, 300_000)
    sb._cache["abc"] = (INTRO, otra_edicion)

    assert res.segmentos_de(FakePlayable()) == (INTRO,)
    assert res.segmentos_de(_de_youtube("sin_cache")) == ()
    assert res.segmentos_de(FakePlayable(source="local", uri="file:///x.wav", identifier="abc")) == ()
    assert sb.preguntados == []


def test_el_video_de_una_pista_sale_del_origen_si_ya_estaba_resuelta():
    assert resolver.video_de(FakePlayable()) == "abc"
    assert resolver.video_de(pista_ya_resuelta()) == "abc"
    assert resolver.video_de(pista_http()) == ""
    assert resolver.video_de(FakePlayable(source="local", uri="file:///x.wav")) == ""


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

    assert uno.sponsorblock is not otro.sponsorblock
    assert uno.sponsorblock.activo is True
