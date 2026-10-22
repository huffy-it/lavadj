"""El cog de música: lo que decide el bot antes de tocarle la puerta a Lavalink.

Los dobles imitan solo lo que el cog realmente usa del player de wavelink: la
cola (con `put_at`, que es lo que hace que `!playnext` funcione), la cola del
autoplay, y el par de banderas de estado.
"""

import asyncio
from types import SimpleNamespace

import pytest
import wavelink
from discord.ext import commands

from cogs import music as music_mod
from cogs.music import Music
from core import config
from core.dj import Clip
from core.locutor import Locutor
from core.reintentos import CAIDAS_SEGUIDAS_MAX, REINTENTOS_POR_PISTA
from core.resolver import Resolver
from core.salud import Salud
from core.spotify import Lista


class FakeTrack:
    def __init__(self, title="Un tema"):
        self.title = title
        self.length = 180_000
        self.identifier = title


class FakeQueue(list):
    async def put_wait(self, track):
        self.append(track)
        return 1

    def put_at(self, index, track):
        self.insert(index, track)

    def get(self):
        return self.pop(0)


class FakePlayer:
    def __init__(self, playing=False, temas=(), auto=(), guild=None):
        self.playing = playing
        self.paused = False
        self.connected = True
        self.queue = FakeQueue(temas)
        self.auto_queue = FakeQueue(auto)
        self.guild = guild or FakeGuild()
        self.channel = "canal-de-voz"
        self.autoplay = wavelink.AutoPlayMode.disabled
        self.tocando = []

    async def play(self, track):
        self.tocando.append(track)
        self.playing = True


class FakeGuild:
    def __init__(self, guild_id=1):
        self.id = guild_id


class FakeVoice:
    def __init__(self, channel):
        self.channel = channel


class FakeAuthor:
    def __init__(self, channel="canal-de-voz", nombre="ana"):
        self.voice = FakeVoice(channel) if channel else None
        self.display_name = nombre


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeCtx:
    def __init__(self, vc=None, author=None, guild=None):
        self.voice_client = vc
        self.author = author or FakeAuthor()
        self.guild = guild or (vc.guild if vc else FakeGuild())
        self.enviados = []

    def typing(self):
        return FakeTyping()

    async def send(self, contenido=None, **kwargs):
        self.enviados.append(contenido)
        return contenido

    @property
    def ultimo(self):
        return self.enviados[-1] if self.enviados else ""


@pytest.fixture
def cog():
    cog = Music(bot=None)
    cog.dj.enabled = False      # sin espeak en los tests
    return cog


def buscador(resultado):
    """Reemplaza a wavelink.Playable.search devolviendo lo que le pidamos."""
    async def _search(query, *a, **kw):
        return resultado
    return _search


# --- playnext ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_playnext_mete_el_tema_al_frente(cog, monkeypatch):
    ya_en_cola = [FakeTrack("vieja 1"), FakeTrack("vieja 2")]
    vc = FakePlayer(playing=True, temas=ya_en_cola)
    ctx = FakeCtx(vc)

    monkeypatch.setattr(Music, "_connect_voice", lambda self, ctx: _listo(vc))
    monkeypatch.setattr(music_mod.wavelink.Playable, "search", buscador([FakeTrack("la nueva")]))

    await cog.playnext(cog, ctx, query="la nueva")

    assert [t.title for t in vc.queue] == ["la nueva", "vieja 1", "vieja 2"]
    assert "La próxima" in ctx.ultimo


@pytest.mark.asyncio
async def test_playnext_sin_nada_sonando_arranca_al_toque(cog, monkeypatch):
    vc = FakePlayer(playing=False)
    ctx = FakeCtx(vc)

    monkeypatch.setattr(Music, "_connect_voice", lambda self, ctx: _listo(vc))
    monkeypatch.setattr(music_mod.wavelink.Playable, "search", buscador([FakeTrack("única")]))

    await cog.playnext(cog, ctx, query="única")

    assert [t.title for t in vc.tocando] == ["única"]


@pytest.mark.asyncio
async def test_playnext_no_se_come_una_playlist_entera(cog, monkeypatch):
    """Meter 100 temas adelante de todo no es lo que nadie espera de "la próxima"."""
    vc = FakePlayer(playing=True)
    ctx = FakeCtx(vc)

    playlist = wavelink.Playlist.__new__(wavelink.Playlist)
    playlist.name = "Una lista larga"
    playlist.tracks = [FakeTrack(str(i)) for i in range(100)]
    monkeypatch.setattr(Music, "_connect_voice", lambda self, ctx: _listo(vc))
    monkeypatch.setattr(music_mod.wavelink.Playable, "search", buscador(playlist))

    await cog.playnext(cog, ctx, query="https://youtube.com/playlist?list=x")

    assert not vc.queue
    assert "`!play`" in ctx.ultimo


@pytest.mark.asyncio
async def test_playnext_respeta_el_tope_de_la_cola(cog, monkeypatch):
    vc = FakePlayer(playing=True, temas=[FakeTrack(str(i)) for i in range(config.QUEUE_LIMIT)])
    ctx = FakeCtx(vc)
    monkeypatch.setattr(Music, "_connect_voice", lambda self, ctx: _listo(vc))

    await cog.playnext(cog, ctx, query="una más")

    assert len(vc.queue) == config.QUEUE_LIMIT
    assert "llena" in ctx.ultimo


# --- autoplay ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_autoplay_on_pone_el_modo_de_wavelink(cog):
    vc = FakePlayer()
    ctx = FakeCtx(vc)

    await cog.autoplay_cmd(cog, ctx, "off")
    cog._aplicar_autoplay(vc)
    assert vc.autoplay is wavelink.AutoPlayMode.partial

    await cog.autoplay_cmd(cog, ctx, "on")
    assert vc.autoplay is wavelink.AutoPlayMode.enabled
    assert cog.autoplay_activo(ctx.guild.id)


@pytest.mark.asyncio
async def test_apagar_el_autoplay_tira_lo_ya_recomendado(cog):
    """Si no, sigue sonando un rato más y parece que el comando no hizo nada."""
    vc = FakePlayer(auto=[FakeTrack("recomendada")])
    ctx = FakeCtx(vc)

    await cog.autoplay_cmd(cog, ctx, "off")

    assert not vc.auto_queue


@pytest.mark.asyncio
async def test_autoplay_sin_argumento_informa_el_estado(cog):
    ctx = FakeCtx(FakePlayer())
    await cog.autoplay_cmd(cog, ctx, "")
    assert "autoplay" in ctx.ultimo.lower()


def test_el_autoplay_es_por_servidor(cog):
    cog._autoplay[7] = False
    assert cog.autoplay_activo(7) is False
    assert cog.autoplay_activo(8) is config.AUTOPLAY_DEFAULT


# --- permisos ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_los_controles_son_de_quien_esta_escuchando():
    predicado = music_mod.en_el_mismo_canal().predicate
    vc = FakePlayer()

    de_afuera = FakeCtx(vc, author=FakeAuthor(channel="otro-canal"))
    with pytest.raises(commands.CheckFailure):
        await predicado(de_afuera)

    assert await predicado(FakeCtx(vc, author=FakeAuthor(channel="canal-de-voz")))


@pytest.mark.asyncio
async def test_sin_bot_conectado_el_control_no_estorba():
    """No hay música ajena que proteger; el comando ya avisa que no hay nada."""
    predicado = music_mod.en_el_mismo_canal().predicate
    assert await predicado(FakeCtx(vc=None, author=FakeAuthor(channel=None)))


@pytest.mark.asyncio
async def test_nada_de_musica_por_privado(cog):
    ctx = FakeCtx()
    ctx.guild = None
    with pytest.raises(commands.NoPrivateMessage):
        await cog.cog_check(ctx)


# --- volumen del DJ ----------------------------------------------------------

@pytest.mark.asyncio
async def test_dj_vol_mueve_el_volumen_de_la_voz(cog):
    ctx = FakeCtx()

    await cog.dj_cmd(cog, ctx, "vol", "40")

    assert cog.dj.volume == 40
    assert "40" in ctx.ultimo


@pytest.mark.asyncio
async def test_dj_vol_sin_numero_solo_informa(cog):
    cog.dj.volume = 120
    ctx = FakeCtx()

    await cog.dj_cmd(cog, ctx, "vol")

    assert cog.dj.volume == 120
    assert "120" in ctx.ultimo


@pytest.mark.asyncio
async def test_dj_vol_con_basura_no_toca_nada(cog):
    ctx = FakeCtx()

    await cog.dj_cmd(cog, ctx, "vol", "fuerte")

    assert cog.dj.volume == music_mod.config.DJ_VOLUME_DEFAULT
    assert "0 y 200" in ctx.ultimo


# --- reintentos cuando Lavalink no puede sacar el audio ----------------------

class FakeCanalTexto:
    def __init__(self):
        self.enviados = []

    async def send(self, contenido=None, **kwargs):
        self.enviados.append(contenido)


class FakePayloadExcepcion:
    def __init__(self, player, track, exception="400"):
        self.player = player
        self.track = track
        self.exception = exception


class FakePayloadInicio:
    def __init__(self, player, track):
        self.player = player
        self.track = track
        self.original = track


@pytest.fixture
def sin_espera(monkeypatch):
    """El respiro entre reintentos no tiene por qué frenar los tests."""
    async def _dormir(_segundos):
        return None
    monkeypatch.setattr(music_mod.asyncio, "sleep", _dormir)


def _reproductor_con_canal():
    vc = FakePlayer(playing=True)
    vc.text_channel = FakeCanalTexto()
    return vc


@pytest.mark.asyncio
async def test_una_pista_que_falla_vuelve_al_frente_de_la_cola(cog, sin_espera):
    vc = _reproductor_con_canal()
    vc.queue.append(FakeTrack("la que sigue"))
    caida = FakeTrack("la que falló")

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, caida))

    assert [t.title for t in vc.queue] == ["la que falló", "la que sigue"]
    assert vc.text_channel.enviados == []        # todavía no hay nada que avisar


@pytest.mark.asyncio
async def test_despues_de_agotar_los_reintentos_la_saltea_y_avisa(cog, sin_espera):
    vc = _reproductor_con_canal()
    caida = FakeTrack("la maldita")

    for _ in range(REINTENTOS_POR_PISTA):
        await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, caida))
    vc.queue.clear()

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, caida))

    assert list(vc.queue) == []                  # ya no se reencola
    assert "la maldita" in vc.text_channel.enviados[-1]


@pytest.mark.asyncio
async def test_el_clip_del_dj_no_se_reintenta(cog, sin_espera):
    vc = _reproductor_con_canal()
    clip = FakeTrack("corte del DJ")
    clip.uri = f"{music_mod.tts.CACHE_DIR}/dj_abc.wav"

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, clip))

    assert list(vc.queue) == []


@pytest.mark.asyncio
async def test_si_la_pista_termina_bien_recupera_todos_sus_intentos(cog, sin_espera, monkeypatch):
    # El panel no es lo que se está probando aquí y necesita un player de verdad.
    async def _sin_panel(self, guild, vc):
        return None
    monkeypatch.setattr(Music, "_post_panel", _sin_panel)

    vc = _reproductor_con_canal()
    pista = FakeTrack("la intermitente")

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, pista))
    await cog.on_wavelink_track_start(FakePayloadInicio(vc, pista))
    # Arrancar no basta: un tema que arranca y revienta al decodificar no puede
    # reiniciar el contador, o se reintenta sin fin.
    assert cog.estacion(vc.guild.id).reintentos != {}

    await cog.on_wavelink_track_end(FakePayloadFin(vc, pista))

    assert cog.estacion(vc.guild.id).reintentos == {}


def _listo(valor):
    """Un futuro ya resuelto, para reemplazar corutinas en los dobles."""
    futuro = asyncio.get_event_loop().create_future()
    futuro.set_result(valor)
    return futuro


def _pista_resuelta(titulo, identifier, id_youtube="abc"):
    """Una pista ya resuelta: identifier propio de la URL, origen de YouTube.

    Cada resolución trae otra URL de googlevideo y por lo tanto otro
    `identifier`; lo único estable entre vueltas es el origen guardado.
    """
    pista = FakeTrack(titulo)
    pista.identifier = identifier
    pista.extras = {"yt_origen": {"identifier": id_youtube, "title": titulo}}
    return pista


@pytest.mark.asyncio
async def test_el_contador_de_reintentos_sigue_a_la_cancion_no_a_la_url(cog, sin_espera):
    """Si contara por el identifier de la pista, reintentaría para siempre.

    Al re-resolver cambia la URL y con ella el identifier, así que el contador
    se reiniciaría en cada vuelta y la canción no se saltearía nunca.
    """
    vc = _reproductor_con_canal()

    for vuelta in range(REINTENTOS_POR_PISTA):
        await cog.on_wavelink_track_exception(
            FakePayloadExcepcion(vc, _pista_resuelta("la maldita", f"rr{vuelta}-url"))
        )
    vc.queue.clear()

    await cog.on_wavelink_track_exception(
        FakePayloadExcepcion(vc, _pista_resuelta("la maldita", "rr9-otra-url"))
    )

    assert list(vc.queue) == []
    assert "la maldita" in vc.text_channel.enviados[-1]


@pytest.mark.asyncio
async def test_un_video_sin_caso_se_saltea_sin_gastar_reintentos(cog, sin_espera, monkeypatch):
    """Un tema bloqueado no mejora reintentando: son treinta segundos de cola
    congelada para algo que no va a sonar nunca."""
    monkeypatch.setattr(cog.resolver, "motivo_definitivo", lambda pista: "está bloqueada")
    vc = _reproductor_con_canal()

    await cog.on_wavelink_track_exception(
        FakePayloadExcepcion(vc, FakeTrack("la bloqueada"))
    )

    assert list(vc.queue) == []
    assert "está bloqueada" in vc.text_channel.enviados[-1]
    assert "la bloqueada" in vc.text_channel.enviados[-1]


@pytest.mark.asyncio
async def test_la_cancion_se_nombra_por_su_titulo_de_youtube(cog, sin_espera):
    """La pista que vuelve de Lavalink se llama "Unknown title".

    El nombre bueno está en el origen guardado, y es el que tiene que leer la
    gente: en los logs de producción los avisos salían todos como
    "Reintento 1/2 de 'Unknown title'".
    """
    vc = _reproductor_con_canal()
    pista = FakeTrack("Unknown title")
    pista.identifier = "rr4-url"
    pista.extras = {"yt_origen": {"identifier": "abc", "title": "Gato por liebre"}}

    for _ in range(REINTENTOS_POR_PISTA + 1):
        await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, pista))

    assert "Gato por liebre" in vc.text_channel.enviados[-1]
    assert "Unknown title" not in vc.text_channel.enviados[-1]


@pytest.mark.asyncio
async def test_el_cog_le_cuenta_a_la_salud_como_viene_la_cosa(cog, sin_espera, monkeypatch):
    """El bot se puede quedar mudo sin caerse, y desde afuera se ve todo verde."""
    async def _sin_panel(self, guild, vc):
        return None
    monkeypatch.setattr(Music, "_post_panel", _sin_panel)
    vc = _reproductor_con_canal()

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, FakeTrack("una")))
    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, FakeTrack("otra")))
    assert cog.salud.estado(al_aire=True, hay_cola=True)["estado"] == "flojo"

    await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("la que zafó")))
    assert cog.salud.estado(al_aire=True, hay_cola=True)["estado"] == "bien"


@pytest.mark.asyncio
async def test_el_corte_del_dj_no_cuenta_como_que_sono_musica(cog, sin_espera, monkeypatch):
    """Si contara, el DJ hablando taparía para siempre que no suena música."""
    async def _sin_panel(self, guild, vc):
        return None
    monkeypatch.setattr(Music, "_post_panel", _sin_panel)
    vc = _reproductor_con_canal()

    clip = FakeTrack("corte del DJ")
    clip.is_dj_clip = True
    antes = cog.salud.sin_sonar
    await cog.on_wavelink_track_start(FakePayloadInicio(vc, clip))

    assert cog.salud.sin_sonar >= antes


@pytest.mark.asyncio
async def test_cuando_se_rinde_con_un_tema_el_dj_lo_dice(cog, sin_espera, monkeypatch):
    """El aviso sale por la voz porque es lo que uno está escuchando."""
    dicho = []

    async def falso_aviso(texto):
        dicho.append(texto)
        return None

    monkeypatch.setattr(cog.dj, "aviso", falso_aviso)
    vc = _reproductor_con_canal()
    pista = FakeTrack("la maldita")

    for _ in range(REINTENTOS_POR_PISTA + 1):
        await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, pista))

    assert dicho, "no dijo nada"
    assert "la maldita" in dicho[0]


@pytest.mark.asyncio
async def test_el_aviso_hablado_va_al_frente_de_la_cola(cog, sin_espera, monkeypatch):
    """Como el tema falló, el player está libre: suena de inmediato."""
    locucion = FakeTrack("clip del aviso")

    async def falso_aviso(texto):
        return Clip(path="/tmp/aviso.wav", text=texto, for_source=None, kind="aviso")

    async def falso_track(self, clip):
        return locucion

    monkeypatch.setattr(cog.dj, "aviso", falso_aviso)
    monkeypatch.setattr(Locutor, "_clip_a_track", falso_track)
    vc = _reproductor_con_canal()
    vc.queue.append(FakeTrack("la que sigue"))

    for _ in range(REINTENTOS_POR_PISTA + 1):
        await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, FakeTrack("la maldita")))

    assert vc.queue[0] is locucion


@pytest.mark.asyncio
async def test_no_dice_nada_si_no_esta_en_un_canal(cog, sin_espera, monkeypatch):
    """Hablarle a nadie es gastar espeak y ensuciar la cola."""
    dicho = []

    async def falso_aviso(texto):
        dicho.append(texto)
        return None

    monkeypatch.setattr(cog.dj, "aviso", falso_aviso)
    vc = _reproductor_con_canal()
    vc.connected = False

    for _ in range(REINTENTOS_POR_PISTA + 1):
        await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, FakeTrack("la maldita")))

    assert dicho == []


@pytest.mark.asyncio
async def test_la_pista_vuelve_a_la_cola_antes_de_ponerse_a_esperar(cog, monkeypatch):
    """wavelink decide qué hacer apenas se muere el tema, y mira la cola.

    Si la reencolamos después de un await, para cuando llegamos el player ya
    miró una cola vacía, se fue a buscar recomendaciones, no encontró ninguna y
    se estacionó. La pista queda ahí sin que nadie la toque nunca: desde afuera
    es "el bot dejó de reproducir".
    """
    vc = _reproductor_con_canal()
    cola_al_dormir = []

    async def espera_espia(_segundos):
        cola_al_dormir.append(list(vc.queue))

    monkeypatch.setattr(music_mod.asyncio, "sleep", espera_espia)

    await cog.on_wavelink_track_exception(
        FakePayloadExcepcion(vc, FakeTrack("la que falló"))
    )

    assert list(vc.queue), "no la reencoló"
    assert all(cola_al_dormir), "se puso a esperar con la cola todavía vacía"


class FakePayloadFin:
    def __init__(self, player, track, reason="finished"):
        self.player = player
        self.track = track
        self.reason = reason


@pytest.mark.asyncio
async def test_el_wav_del_dj_se_borra_cuando_termina_de_sonar(cog, tmp_path, monkeypatch):
    monkeypatch.setattr(music_mod.tts, "CACHE_DIR", tmp_path)
    wav = tmp_path / "dj_abc.wav"
    wav.write_bytes(b"RIFF")
    clip = FakeTrack("corte del DJ")
    clip.uri = str(wav)

    await cog.on_wavelink_track_end(FakePayloadFin(_reproductor_con_canal(), clip))

    assert not wav.exists()


@pytest.mark.asyncio
async def test_una_cancion_no_se_borra_al_terminar(cog, tmp_path, monkeypatch):
    monkeypatch.setattr(music_mod.tts, "CACHE_DIR", tmp_path)
    ajeno = tmp_path.parent / "cancion.wav"
    ajeno.write_bytes(b"RIFF")
    tema = FakeTrack("tema")
    tema.uri = str(ajeno)

    await cog.on_wavelink_track_end(FakePayloadFin(_reproductor_con_canal(), tema))

    assert ajeno.exists()


# --- la cola no se traba por fallos -------------------------------------------

def _falla_como_en_lavalink(cog, vc, pista):
    """El handler del cog y después lo que hace wavelink con ese mismo final.

    En wavelink el `loadFailed` suma uno a `_error_count` y, si ya estaba en
    tres, la cola deja de avanzar. Corre después del handler del cog.
    """
    async def _correr():
        await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, pista))
        trabada = vc._error_count >= music_mod.TOPE_DE_WAVELINK
        vc._error_count += 1
        return trabada
    return _correr()


@pytest.mark.asyncio
async def test_un_tema_que_agota_sus_reintentos_no_traba_la_cola(cog, sin_espera):
    vc = _reproductor_con_canal()
    vc._error_count = 0
    pista = FakeTrack("la maldita")

    for _ in range(REINTENTOS_POR_PISTA + 1):
        assert not await _falla_como_en_lavalink(cog, vc, pista)

    # Lo que suena después (el aviso del DJ, el próximo tema) tiene que avanzar.
    assert vc._error_count < music_mod.TOPE_DE_WAVELINK


@pytest.mark.asyncio
async def test_varios_temas_perdidos_seguidos_paran_la_cola(cog, sin_espera):
    vc = _reproductor_con_canal()
    vc._error_count = 0
    trabadas = []

    for n in range(CAIDAS_SEGUIDAS_MAX):
        pista = FakeTrack(f"caída {n}")
        for _ in range(REINTENTOS_POR_PISTA + 1):
            trabadas.append(await _falla_como_en_lavalink(cog, vc, pista))

    assert trabadas[-1] and not any(trabadas[:-1])
    assert "paro la cola" in vc.text_channel.enviados[-1]


@pytest.mark.asyncio
async def test_una_cancion_entera_corta_la_racha_de_temas_perdidos(cog, sin_espera):
    vc = _reproductor_con_canal()
    vc._error_count = 0

    for n in range(CAIDAS_SEGUIDAS_MAX - 1):
        pista = FakeTrack(f"caída {n}")
        for _ in range(REINTENTOS_POR_PISTA + 1):
            await _falla_como_en_lavalink(cog, vc, pista)
    await cog.on_wavelink_track_end(FakePayloadFin(vc, FakeTrack("una que sí sonó")))

    pista = FakeTrack("otra caída")
    for _ in range(REINTENTOS_POR_PISTA + 1):
        assert not await _falla_como_en_lavalink(cog, vc, pista)


@pytest.mark.asyncio
async def test_play_retoma_una_cola_parada_por_fallos(cog):
    tema = FakeTrack("la que espera")
    vc = FakePlayer(temas=[tema])
    vc._error_count = music_mod.TOPE_DE_WAVELINK
    cog.estacion(vc.guild.id).caidas = CAIDAS_SEGUIDAS_MAX

    await cog._arrancar_si_hace_falta(vc)

    assert vc.tocando == [tema]
    assert vc._error_count == 0
    assert cog.estacion(vc.guild.id).caidas == 0


# --- arranques que se pisan ----------------------------------------------------

class PlayerQueResuelve(FakePlayer):
    """Un `play()` que tarda como el de verdad, que espera a yt-dlp."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.resolviendo = 0
        self.suelta = asyncio.Event()

    async def play(self, track):
        self.resolviendo += 1
        try:
            await self.suelta.wait()
            await super().play(track)
        finally:
            self.resolviendo -= 1


@pytest.mark.asyncio
async def test_dos_arranques_a_la_vez_no_se_comen_un_tema(cog):
    a, b = FakeTrack("a"), FakeTrack("b")
    vc = PlayerQueResuelve(temas=[a, b])

    tareas = [asyncio.create_task(cog._arrancar_si_hace_falta(vc)) for _ in range(2)]
    await asyncio.sleep(0)
    vc.suelta.set()
    await asyncio.gather(*tareas)

    assert vc.tocando == [a]
    assert list(vc.queue) == [b]


@pytest.mark.asyncio
async def test_no_arranca_encima_del_autoplay_que_esta_resolviendo(cog):
    b = FakeTrack("b")
    vc = PlayerQueResuelve(temas=[b])
    vc.resolviendo = 1          # el autoplay ya sacó su tema y espera a yt-dlp
    vc.suelta.set()             # si arrancara igual, que falle y no se cuelgue

    await cog._arrancar_si_hace_falta(vc)

    assert vc.tocando == []
    assert list(vc.queue) == [b]


# --- reinicio del nodo de audio -------------------------------------------------

class PlayerDeSesionVieja(FakePlayer):
    """Un player que quedó apuntando a una sesión de Lavalink que ya no existe.

    Imita lo que `recuperar_players` toca: la voz que se reenvía, el tema en
    curso con su última posición y el `play()` que lo repite.
    """

    def __init__(self, actual=None, posicion=0, **kwargs):
        super().__init__(playing=actual is not None, **kwargs)
        self._current = actual
        self._last_position = posicion
        self._last_update = 2        # Lavalink ya reportó este tema...
        self._inicio_ns = 1          # ...después de que arrancó
        self.paused = False
        self.voces = 0
        self.rechaza_la_voz = False
        self.falla_el_play = False
        self.repetidos = []
        self.guild.voice_client = self

    @property
    def current(self):
        return self._current

    async def _dispatch_voice_update(self):
        self.voces += 1
        if self.rechaza_la_voz:
            self.connected = False      # wavelink desconecta si Lavalink la rechaza

    async def play(self, track, **kwargs):
        if self.falla_el_play:
            raise RuntimeError("Lavalink no responde")
        self.repetidos.append((track, kwargs))

    async def disconnect(self):
        self.connected = False


async def test_un_nodo_reiniciado_retoma_el_tema_donde_iba(cog):
    """Sin esto `playing` queda en verdadero para siempre y la cola no avanza."""
    sonando, siguiente = FakeTrack("sonando"), FakeTrack("siguiente")
    vc = PlayerDeSesionVieja(actual=sonando, posicion=95_000, temas=[siguiente])
    vc.paused = True

    await cog.recuperar_players([vc])

    assert vc.voces == 1
    assert vc.repetidos == [(sonando, {"start": 95_000, "paused": True, "add_history": False})]
    assert list(vc.queue) == [siguiente]
    assert vc.connected


async def test_un_nodo_reiniciado_no_reanuda_desde_la_posicion_del_tema_anterior(cog):
    vc = PlayerDeSesionVieja(actual=FakeTrack("nuevo"), posicion=95_000)
    vc._last_update = 1         # la última actualización es anterior al arranque
    vc._inicio_ns = 2

    await cog.recuperar_players([vc])

    assert vc.repetidos[0][1]["start"] == 0


async def test_un_nodo_reiniciado_con_el_player_ocioso_solo_rehace_la_voz(cog):
    vc = PlayerDeSesionVieja(actual=None)

    await cog.recuperar_players([vc])

    assert vc.voces == 1
    assert vc.repetidos == []
    assert vc.connected


async def test_si_lavalink_rechaza_la_voz_se_corta_y_se_avisa(cog):
    vc = PlayerDeSesionVieja(actual=FakeTrack("sonando"), temas=[FakeTrack("otro")])
    vc.rechaza_la_voz = True
    vc.text_channel = FakeCanalTexto()

    await cog.recuperar_players([vc])

    assert vc.repetidos == []
    assert not vc.connected
    assert list(vc.queue) == []
    assert vc.text_channel.enviados == [music_mod.AVISO_NODO_REINICIADO]


async def test_si_no_se_puede_repetir_el_tema_se_corta_y_avisa_en_el_panel(cog):
    """El aviso va donde está el panel; no se repite en el canal de texto."""
    vc = PlayerDeSesionVieja(actual=FakeTrack("sonando"))
    vc.falla_el_play = True
    vc.text_channel = FakeCanalTexto()
    canal_del_panel = FakeCanalTexto()

    class MensajeFalso:
        channel = canal_del_panel

        async def delete(self):
            pass

    class VistaFalsa:
        def stop(self):
            pass

    cog.estacion(vc.guild.id).panel = (MensajeFalso(), VistaFalsa())

    await cog.recuperar_players([vc])

    assert not vc.connected
    assert canal_del_panel.enviados == [music_mod.AVISO_NODO_REINICIADO]
    assert vc.text_channel.enviados == []


async def test_un_player_roto_no_frena_a_los_demas(cog):
    roto = PlayerDeSesionVieja(actual=FakeTrack("roto"), guild=FakeGuild(1))
    roto.falla_el_play = True
    bueno = PlayerDeSesionVieja(actual=FakeTrack("bueno"), guild=FakeGuild(2))

    await cog.recuperar_players([roto, bueno])

    assert len(bueno.repetidos) == 1


# --- wavs del DJ que no se quedan en disco ---------------------------------------

async def test_al_cortar_se_borran_los_cortes_del_dj_que_esperaban_en_la_cola(cog, tmp_path, monkeypatch):
    monkeypatch.setattr(music_mod.tts, "CACHE_DIR", tmp_path)
    wav = tmp_path / "dj_pendiente.wav"
    wav.write_bytes(b"RIFF")
    clip = FakeTrack("corte")
    clip.uri = str(wav)
    ajeno = tmp_path.parent / "cancion.wav"
    ajeno.write_bytes(b"RIFF")
    cancion = FakeTrack("tema")
    cancion.uri = str(ajeno)
    vc = PlayerDeSesionVieja(temas=[clip, cancion])

    await cog.teardown(vc.guild)

    assert not wav.exists()
    assert ajeno.exists()


@pytest.mark.asyncio
async def test_un_tema_que_falla_antes_del_primer_reporte_igual_se_reintenta(cog, sin_espera):
    """wavelink deja `_last_update` en None hasta el primer playerUpdate: la
    cuenta para reanudar no puede reventar ahí, o el tema se pierde sin reintento."""
    vc = _reproductor_con_canal()
    vc._last_update = None
    vc._inicio_ns = 123
    caida = FakeTrack("la primera de la noche")

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, caida))

    assert list(vc.queue) == [caida]


# --- el hueco entre pistas ----------------------------------------------------

@pytest.fixture
def reloj(cog):
    """La salud del cog con un reloj que solo se mueve cuando uno lo empuja."""
    marca = {"t": 0.0}
    cog.salud = Salud(reloj=lambda: marca["t"])

    def avanzar(segundos):
        marca["t"] += segundos

    return avanzar


@pytest.fixture
def sin_panel(monkeypatch):
    """El panel necesita un player de verdad y aquí no es lo que se prueba."""
    async def _sin_panel(self, guild, vc):
        return None
    monkeypatch.setattr(Music, "_post_panel", _sin_panel)


def _hueco_medido(cog):
    return cog.salud.estado(al_aire=True, hay_cola=True)["hueco"]


@pytest.mark.asyncio
async def test_el_hueco_entre_dos_canciones_queda_registrado(cog, reloj, sin_panel, caplog):
    vc = _reproductor_con_canal()
    vc.queue.append(FakeTrack("la que sigue"))

    with caplog.at_level("INFO", logger="music"):
        await cog.on_wavelink_track_end(FakePayloadFin(vc, FakeTrack("la que terminó")))
        reloj(3.0)
        await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("la que sigue")))

    assert _hueco_medido(cog)["ultimo"] == 3.0
    assert "3.0 s" in caplog.text
    assert "la que sigue" in caplog.text


@pytest.mark.asyncio
async def test_el_hueco_tambien_cuenta_con_un_corte_del_dj(cog, reloj, sin_panel, caplog):
    """El silencio antes de un clip lo oye igual quien escucha."""
    vc = _reproductor_con_canal()
    vc.queue.append(FakeTrack("el corte"))
    clip = FakeTrack("corte del DJ")
    clip.is_dj_clip = True

    with caplog.at_level("INFO", logger="music"):
        await cog.on_wavelink_track_end(FakePayloadFin(vc, FakeTrack("la que terminó")))
        reloj(0.4)
        await cog.on_wavelink_track_start(FakePayloadInicio(vc, clip))

    assert _hueco_medido(cog)["ultimo"] == 0.4
    assert "0.4 s" in caplog.text
    assert "corte del DJ" in caplog.text


@pytest.mark.asyncio
async def test_un_skip_tambien_cuenta_como_hueco(cog, reloj, sin_panel):
    vc = _reproductor_con_canal()

    await cog.on_wavelink_track_end(FakePayloadFin(vc, FakeTrack("saltada"), reason="replaced"))
    reloj(5.0)
    await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("la que sigue")))

    assert _hueco_medido(cog)["ultimo"] == 5.0


@pytest.mark.asyncio
async def test_el_hueco_del_clip_no_pisa_a_sono(cog, reloj, sin_panel):
    """Medir el hueco del clip no puede contar como que sonó música."""
    vc = _reproductor_con_canal()
    vc.queue.append(FakeTrack("la que sigue"))
    clip = FakeTrack("corte del DJ")
    clip.is_dj_clip = True
    reloj(100)

    await cog.on_wavelink_track_end(FakePayloadFin(vc, FakeTrack("la que terminó")))
    await cog.on_wavelink_track_start(FakePayloadInicio(vc, clip))

    assert cog.salud.sin_sonar == 100


@pytest.mark.asyncio
async def test_si_la_cola_queda_vacia_lo_que_arranque_despues_no_es_un_hueco(
    cog, reloj, sin_panel, monkeypatch
):
    """Un `!play` nuevo minutos después no es una transición entre temas."""
    async def _sin_desconexion(self, guild, vc):
        return None
    monkeypatch.setattr(Music, "_schedule_idle_disconnect", _sin_desconexion)
    vc = _reproductor_con_canal()

    await cog.on_wavelink_track_end(FakePayloadFin(vc, FakeTrack("la última")))
    reloj(10.0)
    await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("un !play nuevo")))

    assert _hueco_medido(cog) is None


@pytest.mark.asyncio
async def test_si_la_cola_queda_vacia_se_avisa_a_la_salud(cog, sin_panel, monkeypatch):
    async def _sin_desconexion(self, guild, vc):
        return None
    monkeypatch.setattr(Music, "_schedule_idle_disconnect", _sin_desconexion)
    descartados = []
    monkeypatch.setattr(cog.salud, "sin_siguiente", descartados.append)
    vc = _reproductor_con_canal()

    await cog.on_wavelink_track_end(FakePayloadFin(vc, FakeTrack("la última")))

    assert descartados == [vc.guild.id]


@pytest.mark.asyncio
async def test_al_desconectar_el_final_pendiente_se_descarta(cog, reloj):
    vc = PlayerDeSesionVieja()
    cog.salud.termino(vc.guild.id)

    await cog.teardown(vc.guild)
    reloj(10.0)

    assert cog.salud.arranco(vc.guild.id) is None


# --- el resolver compartido ------------------------------------------------------

def test_el_cog_toma_el_resolver_del_bot():
    compartido = Resolver()
    assert Music(bot=SimpleNamespace(resolver=compartido)).resolver is compartido


def test_sin_resolver_en_el_bot_cada_cog_arma_el_suyo():
    uno, otro = Music(bot=None), Music(bot=None)
    assert isinstance(uno.resolver, Resolver)
    assert uno.resolver is not otro.resolver


class FakeCanalDeVoz:
    """El canal al que se conecta el bot: anota con qué clase de player lo hizo."""

    def __init__(self, player):
        self.player = player
        self.clase = None

    async def connect(self, *, cls):
        self.clase = cls
        return self.player


@pytest.mark.asyncio
async def test_al_conectar_el_player_comparte_el_resolver_del_cog(cog):
    """Si el bot no trae resolver, el player se arma con uno propio y el cog no
    vería los videos sin caso que ese anota: `_connect_voice` los empareja."""
    vc = FakePlayer()
    canal = FakeCanalDeVoz(vc)
    ctx = FakeCtx(author=FakeAuthor(channel=canal))
    ctx.channel = "canal-de-texto"

    assert await cog._connect_voice(ctx) is vc

    assert canal.clase is music_mod.resolver.ResolvingPlayer
    assert vc.resolver is cog.resolver


@pytest.mark.asyncio
async def test_el_cog_saltea_el_video_que_marco_el_resolver_compartido(sin_espera, monkeypatch):
    """El player anota en el resolver que el cog después consulta: es el mismo."""
    compartido = Resolver()
    cog = Music(bot=SimpleNamespace(resolver=compartido))
    cog.dj.enabled = False

    class ProcesoQueFalla:
        returncode = 1

        async def communicate(self):
            return b"", b"ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader"

    async def _exec(*args, **kwargs):
        return ProcesoQueFalla()

    monkeypatch.setattr(music_mod.asyncio, "create_subprocess_exec", _exec)
    await compartido.direct_url("https://www.youtube.com/watch?v=abc")

    pista = FakeTrack("la borrada")
    pista.uri = "https://www.youtube.com/watch?v=abc"
    vc = _reproductor_con_canal()

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, pista))

    assert list(vc.queue) == []
    assert "la borraron" in vc.text_channel.enviados[-1]


# --- precargar la URL del tema que viene -------------------------------------------

class ResolverQueAnota:
    """Un resolver que solo anota qué pistas le pidieron adelantar."""

    def __init__(self):
        self.precargadas = []

    def precargar(self, pista):
        self.precargadas.append(pista)


def _clip_del_dj():
    clip = FakeTrack("corte del DJ")
    clip.is_dj_clip = True
    return clip


@pytest.mark.asyncio
async def test_al_arrancar_una_cancion_se_precarga_la_que_sigue(cog, sin_panel):
    vc = _reproductor_con_canal()
    vc.resolver = ResolverQueAnota()
    siguiente = FakeTrack("la que sigue")
    vc.queue.extend([siguiente, FakeTrack("la de después")])

    await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("la que suena")))

    # Solo la próxima: las demás esperan su turno, porque la URL caduca.
    assert vc.resolver.precargadas == [siguiente]


@pytest.mark.asyncio
async def test_si_la_cola_esta_vacia_se_precarga_la_que_puso_el_autoplay(cog, sin_panel):
    vc = _reproductor_con_canal()
    vc.resolver = ResolverQueAnota()
    sugerida = FakeTrack("la sugerida")
    vc.auto_queue.append(sugerida)

    await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("la que suena")))

    assert vc.resolver.precargadas == [sugerida]


@pytest.mark.asyncio
async def test_al_arrancar_un_corte_del_dj_se_precarga_la_que_viene_despues(cog, sin_panel):
    """El rato de la voz es justo el que hace falta para resolver la que sigue."""
    vc = _reproductor_con_canal()
    vc.resolver = ResolverQueAnota()
    despues_del_corte = FakeTrack("la que viene después del corte")
    vc.queue.append(despues_del_corte)

    await cog.on_wavelink_track_start(FakePayloadInicio(vc, _clip_del_dj()))

    assert vc.resolver.precargadas == [despues_del_corte]


@pytest.mark.asyncio
async def test_sin_nada_que_venga_no_se_precarga(cog, sin_panel):
    vc = _reproductor_con_canal()
    vc.resolver = ResolverQueAnota()

    await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("la que suena")))
    await cog.on_wavelink_track_start(FakePayloadInicio(vc, _clip_del_dj()))

    assert vc.resolver.precargadas == []


@pytest.mark.asyncio
async def test_si_lo_que_viene_es_un_corte_del_dj_no_se_precarga(cog, sin_panel):
    vc = _reproductor_con_canal()
    vc.resolver = ResolverQueAnota()
    vc.queue.append(_clip_del_dj())

    await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("la que suena")))

    assert vc.resolver.precargadas == []


@pytest.mark.asyncio
async def test_sin_resolver_en_el_player_se_usa_el_del_cog(cog, sin_panel):
    """Por si el player no es un `ResolvingPlayer` y no trae resolver propio."""
    vc = _reproductor_con_canal()
    assert not hasattr(vc, "resolver")
    cog.resolver = ResolverQueAnota()
    siguiente = FakeTrack("la que sigue")
    vc.queue.append(siguiente)

    await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("la que suena")))

    assert cog.resolver.precargadas == [siguiente]


@pytest.mark.asyncio
async def test_si_la_precarga_falla_el_panel_sale_igual(cog, monkeypatch, caplog):
    """Es una optimización: no puede llevarse por delante lo que sigue en el listener."""
    publicados = []

    async def _panel(self, guild, vc):
        publicados.append(vc)
    monkeypatch.setattr(Music, "_post_panel", _panel)

    class ResolverQueRevienta:
        def precargar(self, pista):
            raise RuntimeError("sin loop")

    vc = _reproductor_con_canal()
    vc.resolver = ResolverQueRevienta()
    vc.queue.append(FakeTrack("la que sigue"))

    with caplog.at_level("WARNING", logger="music"):
        await cog.on_wavelink_track_start(FakePayloadInicio(vc, FakeTrack("la que suena")))

    assert publicados == [vc]
    assert "precargar" in caplog.text


# --- el vigilante que destraba la cola ---------------------------------------------

class PlayerQueSeQuedaMudo(FakePlayer):
    async def play(self, track):
        self.tocando.append(track)


class PlayerQueRevienta(FakePlayer):
    async def play(self, track):
        raise RuntimeError("Lavalink no responde")


class PlayerQueRevientaAlMirarlo(FakePlayer):
    @property
    def paused(self):
        raise RuntimeError("el player se estaba desconectando")

    @paused.setter
    def paused(self, valor):
        pass


async def _revisar(cog, players, veces):
    """Con tope de tiempo: un vigilante que pisara un arranque esperaría su lock."""
    async def _pasadas():
        for _ in range(veces):
            await cog._vigilar_una_vez(players)
    await asyncio.wait_for(_pasadas(), timeout=1)


async def test_una_sola_revision_atascada_no_actua_pero_dos_seguidas_si(cog):
    tema = FakeTrack("la que espera")
    vc = FakePlayer(temas=[tema])
    await cog._vigilar_una_vez([vc])
    assert vc.tocando == []
    await cog._vigilar_una_vez([vc])
    assert vc.tocando == [tema]


async def test_con_cola_destraba_por_el_arranque_de_siempre_y_suelta_el_freno(cog, caplog):
    primera, segunda = FakeTrack("primera"), FakeTrack("segunda")
    vc = FakePlayer(temas=[primera, segunda])
    vc._error_count = music_mod.TOPE_DE_WAVELINK
    cog.estacion(vc.guild.id).caidas = CAIDAS_SEGUIDAS_MAX
    with caplog.at_level("WARNING", logger="music"):
        await _revisar(cog, [vc], 2)
    assert vc.tocando == [primera]
    assert list(vc.queue) == [segunda]
    assert vc._error_count == 0
    assert cog.estacion(vc.guild.id).caidas == 0
    assert "Nada suena" in caplog.text and "2 en cola" in caplog.text


async def test_con_la_cola_vacia_destraba_con_el_tema_del_autoplay(cog):
    sugerida, otra = FakeTrack("sugerida"), FakeTrack("otra")
    vc = FakePlayer(auto=[sugerida, otra])
    vc._error_count = music_mod.TOPE_DE_WAVELINK
    cog.estacion(vc.guild.id).caidas = CAIDAS_SEGUIDAS_MAX
    await _revisar(cog, [vc], 2)
    assert vc.tocando == [sugerida]
    assert list(vc.auto_queue) == [otra]
    assert vc._error_count == 0
    assert cog.estacion(vc.guild.id).caidas == 0


async def test_lo_que_pidieron_va_antes_que_lo_que_recomendo_el_autoplay(cog):
    pedida, sugerida = FakeTrack("pedida"), FakeTrack("sugerida")
    vc = FakePlayer(temas=[pedida], auto=[sugerida])
    await _revisar(cog, [vc], 2)
    assert vc.tocando == [pedida]
    assert list(vc.auto_queue) == [sugerida]


def _pausado(vc):
    vc.paused = True


def _sonando(vc):
    vc.playing = True


def _resolviendo(vc):
    vc.resolviendo = 1


def _desconectado(vc):
    vc.connected = False


def _sin_nada_que_tocar(vc):
    vc.queue.clear()
    vc.auto_queue.clear()


@pytest.mark.parametrize("estado", [
    pytest.param(_pausado, id="pausado"),
    pytest.param(_sonando, id="sonando"),
    pytest.param(_resolviendo, id="resolviendo"),
    pytest.param(_desconectado, id="desconectado"),
    pytest.param(_sin_nada_que_tocar, id="sin-nada-en-cola"),
])
async def test_el_vigilante_no_toca_lo_que_no_esta_atascado(cog, caplog, estado):
    vc = FakePlayer(temas=[FakeTrack("de la cola")], auto=[FakeTrack("del autoplay")])
    estado(vc)
    with caplog.at_level("WARNING", logger="music"):
        await _revisar(cog, [vc], 4)
    assert vc.tocando == []
    # `_arrancar_si_hace_falta` ya rechaza a un player ocupado: no alcanza con eso.
    assert "Nada suena" not in caplog.text


async def test_con_el_lock_del_servidor_tomado_no_es_un_atasco(cog):
    vc = FakePlayer(temas=[FakeTrack("de la cola")])
    lock = cog.estacion(vc.guild.id).arranque
    await lock.acquire()
    await _revisar(cog, [vc], 4)
    assert vc.tocando == []
    lock.release()
    # Con el lock tomado tampoco se anotó nada: otra vez hacen falta dos.
    await cog._vigilar_una_vez([vc])
    assert vc.tocando == []
    await cog._vigilar_una_vez([vc])
    assert len(vc.tocando) == 1


async def test_no_pisa_un_arranque_que_esta_sintetizando_la_voz_del_dj(cog, monkeypatch):
    primero, segundo = FakeTrack("primero"), FakeTrack("segundo")
    vc = FakePlayer(temas=[primero, segundo])
    sintetizando, suelta = asyncio.Event(), asyncio.Event()

    async def _dj_que_tarda(self, vc, para, session):
        sintetizando.set()
        await suelta.wait()

    monkeypatch.setattr(cog.dj, "available", lambda: True)
    monkeypatch.setattr(Locutor, "encolar", _dj_que_tarda)
    arranque = asyncio.create_task(cog._arrancar_si_hace_falta(vc))
    await sintetizando.wait()
    await _revisar(cog, [vc], 4)
    suelta.set()
    await arranque
    assert vc.tocando == [primero]
    assert list(vc.queue) == [segundo]


async def test_una_transicion_que_se_resuelve_entre_revisiones_no_acumula(cog):
    vc = FakePlayer(temas=[FakeTrack("de la cola")])
    await cog._vigilar_una_vez([vc])
    vc.playing = True
    await cog._vigilar_una_vez([vc])
    vc.playing = False
    await cog._vigilar_una_vez([vc])
    assert vc.tocando == []
    await cog._vigilar_una_vez([vc])
    assert len(vc.tocando) == 1


async def test_despues_de_actuar_hacen_falta_otras_dos_revisiones(cog):
    a, b = FakeTrack("a"), FakeTrack("b")
    vc = PlayerQueSeQuedaMudo(temas=[a, b])
    await _revisar(cog, [vc], 2)
    assert vc.tocando == [a]
    await cog._vigilar_una_vez([vc])
    assert vc.tocando == [a]
    await cog._vigilar_una_vez([vc])
    assert vc.tocando == [a, b]


async def test_cada_servidor_lleva_su_propia_cuenta(cog):
    uno = FakePlayer(temas=[FakeTrack("uno")], guild=FakeGuild(1))
    otro = FakePlayer(temas=[FakeTrack("otro")], guild=FakeGuild(2))
    await cog._vigilar_una_vez([uno])
    await cog._vigilar_una_vez([uno, otro])
    assert len(uno.tocando) == 1
    assert otro.tocando == []


async def test_un_servidor_que_ya_no_esta_pierde_su_marca(cog):
    vc = FakePlayer(temas=[FakeTrack("de la cola")])
    await cog._vigilar_una_vez([vc])
    await cog._vigilar_una_vez([])
    await cog._vigilar_una_vez([vc])
    assert vc.tocando == []


async def test_un_player_sin_servidor_se_ignora(cog):
    vc = FakePlayer(temas=[FakeTrack("de la cola")])
    vc.guild = None
    await _revisar(cog, [vc], 4)
    assert vc.tocando == []


@pytest.mark.parametrize("clase", [PlayerQueRevienta, PlayerQueRevientaAlMirarlo])
async def test_un_player_que_falla_no_frena_la_revision_de_los_demas(cog, caplog, clase):
    roto = clase(temas=[FakeTrack("del roto")], guild=FakeGuild(1))
    bueno = FakePlayer(temas=[FakeTrack("del bueno")], guild=FakeGuild(2))
    with caplog.at_level("ERROR", logger="music"):
        await _revisar(cog, [roto, bueno], 2)
    assert len(bueno.tocando) == 1
    assert "No pude revisar" in caplog.text


# --- `_arrancar_si_hace_falta` y el autoplay ---------------------------------------

async def test_arrancar_sin_el_parametro_nuevo_no_toca_el_autoplay(cog):
    vc = FakePlayer(auto=[FakeTrack("sugerida")])
    await cog._arrancar_si_hace_falta(vc)
    assert vc.tocando == []
    assert len(vc.auto_queue) == 1


async def test_con_cola_el_autoplay_se_queda_quieto_aunque_se_lo_incluya(cog):
    pedida, sugerida = FakeTrack("pedida"), FakeTrack("sugerida")
    vc = FakePlayer(temas=[pedida], auto=[sugerida])
    await cog._arrancar_si_hace_falta(vc, incluir_autoplay=True)
    assert vc.tocando == [pedida]
    assert list(vc.auto_queue) == [sugerida]


async def test_el_corte_del_dj_abre_el_tema_del_autoplay_sin_perderlo(cog, monkeypatch):
    sugerida, corte = FakeTrack("sugerida"), _clip_del_dj()
    vc = FakePlayer(auto=[sugerida])
    presentados = []

    async def _dj(self, vc, para, session):
        presentados.append(para)
        vc.queue.put_at(0, corte)

    monkeypatch.setattr(cog.dj, "available", lambda: True)
    monkeypatch.setattr(Locutor, "encolar", _dj)
    await cog._arrancar_si_hace_falta(vc, incluir_autoplay=True)
    assert presentados == [sugerida]
    assert vc.tocando == [corte]
    assert list(vc.auto_queue) == [sugerida]


# --- el bucle del vigilante ----------------------------------------------------------

async def test_el_vigilante_arranca_al_cargar_y_se_cancela_al_descargar(cog, monkeypatch):
    monkeypatch.setattr(music_mod.DJ, "load_feed_urls", staticmethod(lambda: []))
    await cog.cog_load()
    vigilante = cog._vigilante
    assert vigilante is not None and not vigilante.done()
    await cog.cog_unload()
    await asyncio.wait_for(asyncio.gather(vigilante, return_exceptions=True), timeout=1)
    assert vigilante.cancelled()


async def test_un_error_en_la_revision_no_mata_el_bucle(cog, monkeypatch, caplog):
    esperar = asyncio.sleep
    revisiones = []

    async def _sin_esperar(_segundos):
        await esperar(0)

    async def _revision(players):
        revisiones.append(players)
        if len(revisiones) == 1:
            raise RuntimeError("se cayó algo")

    monkeypatch.setattr(music_mod.asyncio, "sleep", _sin_esperar)
    monkeypatch.setattr(cog, "_vigilar_una_vez", _revision)
    with caplog.at_level("ERROR", logger="music"):
        bucle = asyncio.create_task(cog._vigilar())
        for _ in range(10):
            await esperar(0)
        bucle.cancel()
        await asyncio.gather(bucle, return_exceptions=True)
    assert len(revisiones) > 1
    assert "Falló una revisión" in caplog.text


def test_sin_bot_no_hay_players_que_vigilar(cog):
    assert cog._players_del_bot() == []


def test_el_vigilante_solo_mira_los_players_de_wavelink(cog):
    player = wavelink.Player.__new__(wavelink.Player)
    cog.bot = SimpleNamespace(voice_clients=[player, object()])
    assert cog._players_del_bot() == [player]


# --- reanudar donde se cortó ---------------------------------------------------------

def _con_reporte_de_lavalink(vc, posicion):
    """Lavalink ya reportó este tema (`_last_update` posterior al arranque)."""
    vc.reanudar = {}
    vc._last_update, vc._inicio_ns, vc._last_position = 2, 1, posicion
    return vc


@pytest.mark.asyncio
async def test_un_reintento_a_mitad_de_tema_reanuda_desde_el_corte_menos_un_respiro(cog):
    vc = _con_reporte_de_lavalink(_reproductor_con_canal(), 60_000)
    pista = FakeTrack("la que se cortó")

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, pista))

    assert vc.reanudar == {"la que se cortó": 58_000}
    assert list(vc.queue) == [pista]


@pytest.mark.asyncio
async def test_si_lavalink_no_reporto_este_tema_el_reintento_arranca_de_cero(cog):
    vc = _con_reporte_de_lavalink(_reproductor_con_canal(), 60_000)
    vc._last_update, vc._inicio_ns = 1, 2       # el último reporte es del tema anterior

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, FakeTrack("la que se cortó")))

    assert vc.reanudar == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("posicion", [5_000, 165_000], ids=["recien-empezada", "casi-terminada"])
async def test_un_corte_muy_temprano_o_muy_tardio_no_se_reanuda(cog, posicion):
    vc = _con_reporte_de_lavalink(_reproductor_con_canal(), posicion)

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, FakeTrack("la que se cortó")))

    assert vc.reanudar == {}


@pytest.mark.asyncio
async def test_un_player_sin_tabla_de_reanudacion_igual_reintenta(cog):
    vc = _reproductor_con_canal()
    vc._last_update, vc._inicio_ns, vc._last_position = 2, 1, 60_000
    assert not hasattr(vc, "reanudar")
    pista = FakeTrack("la que se cortó")

    await cog.on_wavelink_track_exception(FakePayloadExcepcion(vc, pista))

    assert list(vc.queue) == [pista]


# --- listas de Spotify ---------------------------------------------------------------------

def _con_busqueda(monkeypatch, sin_resultado=()):
    """`wavelink.Playable.search` que encuentra todo, salvo lo que se le indique."""
    async def _search(consulta, *a, **kw):
        return [] if consulta in sin_resultado else [FakeTrack(consulta)]
    monkeypatch.setattr(music_mod.wavelink.Playable, "search", _search)


def _con_lista_de_spotify(monkeypatch, *consultas, nombre="una lista"):
    async def _fetch(session, url, limit=100):
        return Lista(kind="playlist", name=nombre, queries=list(consultas))
    monkeypatch.setattr(music_mod.spotify, "fetch", _fetch)


@pytest.mark.asyncio
async def test_un_link_de_spotify_de_un_solo_tema_suena_al_toque(cog, monkeypatch):
    vc = FakePlayer()
    ctx = FakeCtx(vc)
    cog._session = object()
    _con_busqueda(monkeypatch)
    _con_lista_de_spotify(monkeypatch, "Artista - Tema")

    await cog._play_spotify(ctx, vc, "https://open.spotify.com/track/x")

    assert [t.title for t in vc.tocando] == ["Artista - Tema"]
    assert vc.tocando[0].requester == "ana"
    assert ctx.ultimo == "🎧 Spotify — **Artista - Tema**"
    assert cog.estacion(ctx.guild.id).spotify is None


@pytest.mark.asyncio
async def test_una_lista_de_spotify_arranca_con_el_primero_y_carga_el_resto_de_fondo(cog, monkeypatch):
    vc = FakePlayer()
    ctx = FakeCtx(vc)
    cog._session = object()
    _con_busqueda(monkeypatch, sin_resultado=("c",))
    _con_lista_de_spotify(monkeypatch, "a", "b", "c", "d")

    await cog._play_spotify(ctx, vc, "https://open.spotify.com/playlist/x")

    assert [t.title for t in vc.tocando] == ["a"]
    assert "buscando 4 temas" in ctx.enviados[-1]
    carga = cog.estacion(ctx.guild.id).spotify
    assert carga is not None
    await carga

    assert [t.title for t in vc.queue] == ["b", "d"]
    assert ctx.ultimo == "🎧 Listo: 2 temas más en la cola. No encontré 1 en YouTube."
    assert cog.estacion(ctx.guild.id).spotify is None


@pytest.mark.asyncio
async def test_si_el_player_se_va_mientras_carga_la_lista_no_hay_resumen(cog, monkeypatch):
    vc = FakePlayer()
    vc.connected = False
    ctx = FakeCtx(vc)
    _con_busqueda(monkeypatch)
    est = cog.estacion(ctx.guild.id)
    est.spotify = asyncio.current_task()

    await cog._encolar_spotify(ctx, vc, ["a", "b"], "ana")

    assert ctx.enviados == []
    assert est.spotify is None


async def test_una_lista_reemplazada_no_borra_la_referencia_de_la_nueva(cog, monkeypatch):
    vc = FakePlayer()
    vc.connected = False
    ctx = FakeCtx(vc)
    _con_busqueda(monkeypatch)
    est = cog.estacion(ctx.guild.id)
    est.spotify = nueva = object()
    await cog._encolar_spotify(ctx, vc, ["a"], "ana")
    assert est.spotify is nueva


@pytest.mark.asyncio
async def test_cancelar_la_carga_de_spotify_suelta_la_tarea_de_la_estacion(cog, monkeypatch):
    vc = FakePlayer()
    ctx = FakeCtx(vc)
    buscando = asyncio.Event()

    async def _search(consulta, *a, **kw):
        buscando.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(music_mod.wavelink.Playable, "search", _search)
    est = cog.estacion(ctx.guild.id)
    est.spotify = tarea = asyncio.create_task(cog._encolar_spotify(ctx, vc, ["a"], "ana"))
    await buscando.wait()

    est.cancelar_spotify()

    await asyncio.wait_for(asyncio.gather(tarea, return_exceptions=True), timeout=1)
    assert tarea.cancelled()
    assert est.spotify is None
    assert ctx.enviados == []
