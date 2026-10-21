"""El resolver: yt-dlp saca la URL, Lavalink la reproduce.

Lo que se protege aquí es que la pista que termina sonando se siga *viendo* como
la de YouTube (el panel y el DJ leen esos datos) y que ningún tropiezo de yt-dlp
tumbe la reproducción: ante la duda, se devuelve la pista original.
"""

import pytest

from core import resolver


class FakePlayable:
    """Lo mínimo que resolver.py le pide a un wavelink.Playable."""

    def __init__(self, source="youtube", uri="https://www.youtube.com/watch?v=abc",
                 title="Un tema", author="Alguien", identifier="abc"):
        self.source = source
        self.uri = uri
        self.title = title
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
async def test_lo_que_no_es_de_youtube_pasa_derecho(monkeypatch):
    async def _no_llamar(url):
        raise AssertionError("no había que resolver nada")
    monkeypatch.setattr(resolver, "direct_url", _no_llamar)

    clip = FakePlayable(source="local", uri="/tmp/dj_abc.wav")
    assert await resolver.resolve(clip) is clip


@pytest.mark.asyncio
async def test_si_yt_dlp_no_puede_se_devuelve_la_original(monkeypatch):
    async def _sin_url(url):
        return None
    monkeypatch.setattr(resolver, "direct_url", _sin_url)

    pista = FakePlayable()
    assert await resolver.resolve(pista) is pista


@pytest.mark.asyncio
async def test_si_lavalink_no_carga_la_url_se_devuelve_la_original(monkeypatch):
    async def _url(url):
        return "https://rr4.googlevideo.com/videoplayback?x=1"
    async def _vacio(identifier):
        return []
    monkeypatch.setattr(resolver, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _vacio)
    monkeypatch.setattr(resolver, "PAUSA_ENTRE_CARGAS", 0)

    pista = FakePlayable()
    assert await resolver.resolve(pista) is pista


@pytest.mark.asyncio
async def test_si_lavalink_falla_una_vez_se_reintenta_con_la_misma_url(monkeypatch):
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
    monkeypatch.setattr(resolver, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _fetch)
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: datos)

    pista = FakePlayable()
    assert await resolver.resolve(pista) is not pista
    assert pedidas == ["https://rr4.googlevideo.com/videoplayback?x=1"] * 2


@pytest.mark.asyncio
async def test_si_lavalink_sigue_fallando_se_rinde_tras_los_intentos(monkeypatch):
    monkeypatch.setattr(resolver, "PAUSA_ENTRE_CARGAS", 0)
    pedidas = []
    async def _url(url):
        return "https://rr4.googlevideo.com/videoplayback?x=1"
    async def _fetch(identifier):
        pedidas.append(identifier)
        raise RuntimeError("Read timed out")
    monkeypatch.setattr(resolver, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _fetch)

    pista = FakePlayable()
    assert await resolver.resolve(pista) is pista
    assert len(pedidas) == resolver.INTENTOS_DE_CARGA


# --- direct_url --------------------------------------------------------------

@pytest.mark.asyncio
async def test_direct_url_se_queda_con_la_linea_de_la_url(monkeypatch):
    _falso_proceso(monkeypatch, salida=b"algo de ruido\nhttps://rr4.googlevideo.com/v?x=1\n")
    assert await resolver.direct_url("https://youtu.be/abc") == "https://rr4.googlevideo.com/v?x=1"


@pytest.mark.asyncio
async def test_direct_url_devuelve_none_si_yt_dlp_falla(monkeypatch):
    _falso_proceso(monkeypatch, salida=b"", error=b"ERROR: Sign in to confirm", codigo=1)
    assert await resolver.direct_url("https://youtu.be/abc") is None


@pytest.mark.asyncio
async def test_direct_url_sin_url_no_llama_a_nada(monkeypatch):
    def _no_llamar(*a, **kw):
        raise AssertionError("no había que lanzar yt-dlp")
    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _no_llamar)
    assert await resolver.direct_url("") is None


@pytest.mark.asyncio
async def test_el_texto_va_como_argumento_nunca_por_shell(monkeypatch):
    """Las URLs vienen de YouTube: si pasaran por una shell serían inyectables."""
    argumentos = _falso_proceso(monkeypatch, salida=b"https://rr4.googlevideo.com/v\n")
    await resolver.direct_url("https://youtu.be/abc; rm -rf /")
    assert argumentos["args"][-1] == "https://youtu.be/abc; rm -rf /"
    assert argumentos["args"][0] == resolver.config.YTDLP_BIN


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
async def test_el_reintento_pide_una_url_nueva_con_la_uri_de_youtube(monkeypatch):
    """El bug que se está arreglando: sin esto se repetía la URL ya muerta."""
    pedidas = []

    async def _url(url):
        pedidas.append(url)
        return "https://rr9.googlevideo.com/videoplayback?fresca=1"

    async def _encontrar(identifier):
        return [pista_http()]

    monkeypatch.setattr(resolver, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _encontrar)
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: datos)

    await resolver.resolve(pista_ya_resuelta())

    assert pedidas == ["https://www.youtube.com/watch?v=abc"]


@pytest.mark.asyncio
async def test_la_pista_reintentada_no_pierde_la_cara(monkeypatch):
    """En el reintento no se le puede copiar la cara a partir de sí misma.

    La pista que vuelve de Lavalink ya está degradada ("Unknown title", uri de
    googlevideo), así que los datos que se ven salen del origen guardado. Si no,
    a la segunda vuelta el panel mostraría "Unknown title" para siempre.
    """
    async def _url(url):
        return "https://rr9.googlevideo.com/videoplayback?fresca=1"

    async def _encontrar(identifier):
        return [pista_http()]

    monkeypatch.setattr(resolver, "direct_url", _url)
    monkeypatch.setattr(resolver.wavelink.Pool, "fetch_tracks", _encontrar)
    monkeypatch.setattr(resolver.wavelink, "Playable", lambda datos: datos)

    datos = await resolver.resolve(pista_ya_resuelta())

    assert datos["info"]["title"] == "Un tema"
    assert datos["info"]["identifier"] == "abc"
    # Y el origen se vuelve a llevar: tiene que aguantar todas las vueltas que dé.
    assert datos["userData"][resolver.CLAVE_ORIGEN]["uri"] == "https://www.youtube.com/watch?v=abc"


# --- fallos que no tienen arreglo --------------------------------------------

@pytest.mark.asyncio
async def test_un_video_bloqueado_queda_marcado_como_definitivo(monkeypatch):
    """Reintentar un video bloqueado no va a servir nunca: hay que saltearlo ya."""
    resolver.olvidar_motivos()
    _falso_proceso(
        monkeypatch, salida=b"", codigo=1,
        error=b"ERROR: [youtube] abc: It was blocked due to the claimed content by Paramount Global (PMN).",
    )

    assert await resolver.direct_url("https://www.youtube.com/watch?v=abc") is None
    assert resolver.motivo_definitivo(FakePlayable()) == "está bloqueada"


@pytest.mark.asyncio
async def test_un_tropiezo_pasajero_no_se_marca(monkeypatch):
    """Un 400 o un timeout sí mejoran reintentando: no hay que quemarlos."""
    resolver.olvidar_motivos()
    _falso_proceso(
        monkeypatch, salida=b"", codigo=1,
        error=b"ERROR: unable to download video data: HTTP Error 400: Bad Request",
    )

    await resolver.direct_url("https://www.youtube.com/watch?v=abc")
    assert resolver.motivo_definitivo(FakePlayable()) is None


@pytest.mark.asyncio
async def test_el_motivo_se_reconoce_por_el_origen_guardado(monkeypatch):
    """En el reintento la pista llega con la uri de googlevideo, no la de YouTube."""
    resolver.olvidar_motivos()
    _falso_proceso(
        monkeypatch, salida=b"", codigo=1,
        error=b"ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
    )

    await resolver.direct_url("https://www.youtube.com/watch?v=abc")
    assert resolver.motivo_definitivo(pista_ya_resuelta()) == "la borraron"


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
async def test_revisar_ytdlp_deja_el_estado_a_mano_del_panel(monkeypatch):
    _falso_proceso(monkeypatch, salida=b"2026.07.04\n")
    from datetime import date
    monkeypatch.setattr(resolver, "_hoy", lambda: date(2026, 8, 28))

    await resolver.revisar_ytdlp()

    assert resolver.estado_ytdlp() == {"version": "2026.07.04", "dias": 55, "viejo": True}


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


async def test_el_player_avisa_que_esta_resolviendo(monkeypatch):
    """Mientras yt-dlp trabaja, `playing` da falso: `resolviendo` es lo que
    impide que el cog largue otro tema encima."""
    import wavelink

    vc = resolver.ResolvingPlayer.__new__(resolver.ResolvingPlayer)
    vc.reanudar = {}
    vc.resolviendo = 0
    visto = []

    async def _resolve(pista):
        visto.append(vc.resolviendo)
        return pista

    async def _play(self, pista, *a, **kw):
        visto.append(vc.resolviendo)
        return pista

    monkeypatch.setattr(resolver, "resolve", _resolve)
    monkeypatch.setattr(wavelink.Player, "play", _play)
    monkeypatch.setattr(wavelink.Player, "connected", property(lambda self: True))

    await vc.play(FakePlayable())

    assert visto == [1, 1]
    assert vc.resolviendo == 0


# --- yt-dlp que no se puede lanzar ---------------------------------------------

@pytest.mark.parametrize("error", [FileNotFoundError("no existe"), PermissionError("sin permiso")])
async def test_direct_url_con_el_binario_roto_devuelve_none(monkeypatch, error):
    """Un OSError al lanzar yt-dlp no puede escapar: en el autoplay mataría la cola."""
    async def _exec(*args, **kwargs):
        raise error

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)
    assert await resolver.direct_url("https://youtu.be/abc") is None


async def test_resolve_cae_a_la_pista_original_si_yt_dlp_no_se_puede_lanzar(monkeypatch):
    async def _exec(*args, **kwargs):
        raise FileNotFoundError("no existe")

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", _exec)
    pista = FakePlayable()
    assert await resolver.resolve(pista) is pista


# --- play() después de desconectarse ---------------------------------------------

def _player_que_resuelve(monkeypatch, conectado):
    import wavelink

    vc = resolver.ResolvingPlayer.__new__(resolver.ResolvingPlayer)
    vc.reanudar = {}
    vc.resolviendo = 0
    llamadas = []

    async def _resolve(pista):
        return pista

    async def _play(self, pista, *a, **kw):
        llamadas.append(pista)
        return pista

    monkeypatch.setattr(resolver, "resolve", _resolve)
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
