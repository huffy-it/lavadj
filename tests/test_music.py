"""El cog de música: lo que decide el bot antes de tocarle la puerta a Lavalink.

Los dobles imitan solo lo que el cog realmente usa del player de wavelink: la
cola (con `put_at`, que es lo que hace que `!playnext` funcione), la cola del
autoplay, y el par de banderas de estado.
"""

import asyncio
import re
import time
from types import SimpleNamespace

import discord
import pytest
import wavelink
from discord.ext import commands

from cogs import music as music_mod
from cogs.music import Music
from core import api, config, persistencia
from core.dj import Clip
from core.letra_en_vivo import HABLA_EL_DJ, SIN_TIEMPOS, armar_texto
from core.locutor import Locutor
from core.lyrics import Letra
from core.panel import PlayerPanel
from core.reintentos import CAIDAS_SEGUIDAS_MAX, REINTENTOS_POR_PISTA
from core.resolver import Resolver
from core.salud import Salud
from core.spotify import Lista
from core.sponsorblock import Segmento, SponsorBlock


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
    def __init__(self, vc=None, author=None, guild=None, interaction=None):
        self.voice_client = vc
        self.author = author or FakeAuthor()
        self.guild = guild or (vc.guild if vc else FakeGuild())
        self.interaction = interaction
        self.enviados = []
        self.opciones = []
        self.eventos = []

    def typing(self):
        return FakeTyping()

    async def defer(self, *, ephemeral=False):
        self.eventos.append("defer")

    async def send(self, contenido=None, **kwargs):
        self.enviados.append(contenido)
        self.opciones.append(kwargs)
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


# --- la cola en disco ------------------------------------------------------------------

def _raw(titulo, uri=None):
    return {
        "encoded": f"enc-{titulo}",
        "info": {
            "identifier": titulo, "isSeekable": True, "author": "alguien", "length": 180_000,
            "isStream": False, "position": 0, "title": titulo,
            "uri": uri or f"https://www.youtube.com/watch?v={titulo}", "sourceName": "youtube",
        },
        "pluginInfo": {},
        "userData": {},
    }


def _foto_guardada(**cambios):
    foto = {
        "voz": 10, "texto": 20, "actual": _raw("sonando"), "posicion": 95_000,
        "cola": [_raw("uno"), _raw("dos")], "modo": "loop", "volumen": 55,
        "pausado": True, "autoplay": False, "hora": time.time(),
    }
    return {**foto, **cambios}


class ColaConModo(FakeQueue):
    mode = wavelink.QueueMode.normal


class PlayerRetomable(FakePlayer):
    """Un player de wavelink recién conectado, con lo que `retomar_guardadas` le toca."""

    def __init__(self, guild):
        super().__init__(guild=guild)
        self.queue = ColaConModo()
        self.volume = 100
        self.current = None
        self.llamadas = []
        self.falla_el_play = False

    async def play(self, track, **kwargs):
        if self.falla_el_play:
            raise RuntimeError("Lavalink no responde")
        self.llamadas.append((track, kwargs))
        self.current = track
        self.playing = True

    async def set_volume(self, valor):
        self.volume = valor

    async def disconnect(self):
        self.connected = False
        self.guild.voice_client = None


class CanalDeVozRetomable:
    def __init__(self, id_, guild, humanos):
        self.id = id_
        self.guild = guild
        self.members = [SimpleNamespace(bot=False)] * humanos + [SimpleNamespace(bot=True)]
        self.conexiones = 0
        self.clase = None
        self.player = None
        self.falla_al_conectar = False

    async def connect(self, *, cls):
        self.conexiones += 1
        if self.falla_al_conectar:
            raise RuntimeError("timeout")
        self.clase = cls
        self.player = PlayerRetomable(self.guild)
        self.guild.voice_client = self.player
        return self.player


class ServidorRetomable(FakeGuild):
    def __init__(self, guild_id):
        super().__init__(guild_id)
        self.voice_client = None
        self.canales = {}

    def get_channel(self, id_):
        return self.canales.get(id_)

    get_channel_or_thread = get_channel


class DiscordFalso:
    """Lo que el cog le pregunta al bot al retomar: los servidores y cuándo está listo."""

    def __init__(self):
        self.guilds = {}
        self.listo = False
        self.voice_clients = []

    def servidor(self, guild_id, voz=10, texto=20, humanos=1):
        guild = ServidorRetomable(guild_id)
        guild.canales[voz] = CanalDeVozRetomable(voz, guild, humanos)
        guild.canales[texto] = FakeCanalTexto()
        guild.canales[texto].id = texto
        self.guilds[guild_id] = guild
        return guild

    def get_guild(self, guild_id):
        return self.guilds.get(guild_id)

    async def wait_until_ready(self):
        self.listo = True


@pytest.fixture
def discord_falso(cog, monkeypatch):
    monkeypatch.setattr(music_mod.config, "SERVIDORES_PERMITIDOS", frozenset())
    cog.bot = DiscordFalso()
    return cog.bot


@pytest.fixture
def ticks(monkeypatch):
    """`asyncio.sleep` que solo cede el turno: cada vuelta de un bucle es un tick."""
    esperar = asyncio.sleep

    async def _ceder(_segundos):
        await esperar(0)

    monkeypatch.setattr(music_mod.asyncio, "sleep", _ceder)
    return esperar


@pytest.fixture
def escrituras(monkeypatch):
    """Las fotos que se escriben a disco, una por escritura."""
    hechas = []
    original = persistencia.guardar

    def _espia(fotos, *args, **kwargs):
        hechas.append(fotos)
        return original(fotos, *args, **kwargs)

    monkeypatch.setattr(persistencia, "guardar", _espia)
    return hechas


async def _correr_ticks(cog, ticks, veces=10):
    tarea = asyncio.create_task(cog._guardar_en_bucle())
    for _ in range(veces):
        await ticks(0)
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)


def _player_con_cola(guild_id=1, actual="sonando", cola=("uno", "dos")):
    """Un player conectado con tema y cola."""
    vc = PlayerRetomable(FakeGuild(guild_id))
    vc.channel = SimpleNamespace(id=10)
    vc.text_channel = SimpleNamespace(id=20)
    vc.current = wavelink.Playable(_raw(actual)) if actual else None
    vc.queue.extend(wavelink.Playable(_raw(t)) for t in cola)
    vc.guild.voice_client = vc
    return vc


@pytest.fixture
def jugadores(cog):
    """Los players que el cog ve conectados; los tests los agregan con `.nuevo(...)`."""
    class Conectados(list):
        def nuevo(self, *args, **kwargs):
            self.append(_player_con_cola(*args, **kwargs))
            return self[-1]

    lista = Conectados()
    cog._players_del_bot = lambda: list(lista)
    return lista


def _en_disco():
    return persistencia.cargar()


# --- la tarea que fotografía ---------------------------------------------------------------

async def test_la_tarea_de_guardado_arranca_al_cargar_y_se_cancela_al_descargar(cog, monkeypatch):
    monkeypatch.setattr(music_mod.DJ, "load_feed_urls", staticmethod(lambda: []))
    await cog.cog_load()
    guardador = cog._guardador
    assert guardador is not None and not guardador.done()
    assert guardador in cog._tareas
    await cog.cog_unload()
    await asyncio.wait_for(asyncio.gather(guardador, return_exceptions=True), timeout=1)
    assert guardador.cancelled()


async def test_solo_escribe_cuando_algo_cambio(jugadores, cog, escrituras):
    vc = jugadores.nuevo()

    cog._guardar(siempre=False)
    cog._guardar(siempre=False)
    assert len(escrituras) == 1

    vc.queue.append(wavelink.Playable(_raw("tres")))
    cog._guardar(siempre=False)
    cog._guardar(siempre=False)
    assert len(escrituras) == 2
    assert [p["info"]["title"] for p in _en_disco()[1]["cola"]] == ["uno", "dos", "tres"]


async def test_sin_cambios_igual_reescribe_cada_refrescar_cada(jugadores, cog, escrituras):
    jugadores.nuevo()
    cog._guardar(siempre=False)
    cog._guardar(siempre=False)
    assert len(escrituras) == 1
    cog._escrita -= music_mod.REFRESCAR_CADA
    cog._guardar(siempre=False)
    assert len(escrituras) == 2


def test_una_pista_guardada_conserva_quien_la_pidio():
    pista = Music._pista({**_raw("tema"), "pedido_por": "ana"})
    assert pista.requester == "ana"
    assert pista.title == "tema"


async def test_avanzar_el_tema_no_escribe_pero_cambiar_de_tema_si(jugadores, cog, escrituras):
    vc = jugadores.nuevo()
    cog._guardar(siempre=False)

    vc._last_update, vc._inicio_ns, vc._last_position = 2, 1, 40_000
    cog._guardar(siempre=False)
    assert len(escrituras) == 1

    vc.current = vc.queue.get()
    cog._guardar(siempre=False)
    assert len(escrituras) == 2


async def test_pausar_o_cambiar_volumen_o_modo_o_autoplay_cuentan_como_cambio(jugadores, cog, escrituras):
    vc = jugadores.nuevo()
    cog._guardar(siempre=False)

    vc.paused = True
    cog._guardar(siempre=False)
    vc.volume = 30
    cog._guardar(siempre=False)
    vc.queue.mode = wavelink.QueueMode.loop
    cog._guardar(siempre=False)
    cog._autoplay[1] = not cog.autoplay_activo(1)
    cog._guardar(siempre=False)

    assert len(escrituras) == 5


async def test_los_cortes_del_dj_no_van_al_archivo(jugadores, cog, tmp_path, monkeypatch):
    monkeypatch.setattr(music_mod.tts, "CACHE_DIR", tmp_path)
    vc = jugadores.nuevo(cola=())
    vc.queue.append(wavelink.Playable(_raw("corte", uri=str(tmp_path / "corte.wav"))))
    vc.queue.append(wavelink.Playable(_raw("tema")))

    cog._guardar(siempre=False)

    assert [p["info"]["title"] for p in _en_disco()[1]["cola"]] == ["tema"]


async def test_un_servidor_que_no_se_puede_fotografiar_no_frena_a_los_demas(jugadores, cog, caplog):
    roto = jugadores.nuevo(guild_id=1)
    roto.queue.mode = None
    jugadores.nuevo(guild_id=2)

    with caplog.at_level("ERROR", logger="music"):
        cog._guardar(siempre=False)

    assert list(_en_disco()) == [2]
    assert "No pude fotografiar la cola" in caplog.text


async def test_un_player_desconectado_o_sin_nada_que_retomar_no_va_al_archivo(jugadores, cog):
    jugadores.nuevo(guild_id=1).connected = False
    jugadores.nuevo(guild_id=2, actual=None, cola=())
    jugadores.nuevo(guild_id=3)

    cog._guardar(siempre=False)

    assert list(_en_disco()) == [3]


async def test_el_bucle_escribe_una_vez_y_luego_solo_si_cambia(jugadores, cog, ticks, escrituras):
    vc = jugadores.nuevo()
    cog._persistencia = "activa"

    await _correr_ticks(cog, ticks)
    assert len(escrituras) == 1

    vc.queue.clear()
    await _correr_ticks(cog, ticks)
    assert len(escrituras) == 2


async def test_un_error_al_escribir_no_mata_el_bucle_y_se_reintenta(jugadores, cog, ticks, monkeypatch, caplog):
    jugadores.nuevo()
    cog._persistencia = "activa"
    fallos = []
    original = persistencia.guardar

    def _guardar(fotos, *args, **kwargs):
        if not fallos:
            fallos.append(1)
            raise OSError("disco lleno")
        return original(fotos, *args, **kwargs)

    monkeypatch.setattr(persistencia, "guardar", _guardar)
    with caplog.at_level("ERROR", logger="music"):
        await _correr_ticks(cog, ticks)

    assert "No pude guardar las colas" in caplog.text
    assert list(_en_disco()) == [1]


async def test_hasta_retomar_lo_guardado_el_bucle_no_pisa_el_archivo(cog, ticks):
    persistencia.guardar({7: _foto_guardada()})
    assert cog._persistencia == "esperando"

    await _correr_ticks(cog, ticks)

    assert list(_en_disco()) == [7]


async def test_al_terminar_de_retomar_el_bucle_ya_escribe(cog, ticks, discord_falso):
    persistencia.guardar({7: _foto_guardada()})

    await cog.retomar_guardadas()
    await _correr_ticks(cog, ticks)

    assert _en_disco() == {}


# --- teardown ----------------------------------------------------------------------------------

async def test_teardown_borra_la_foto_de_ese_servidor(cog):
    persistencia.guardar({1: _foto_guardada(), 2: _foto_guardada()})
    vc = PlayerDeSesionVieja(guild=FakeGuild(1))

    await cog.teardown(vc.guild)

    assert list(_en_disco()) == [2]


async def test_teardown_sin_player_tambien_borra_la_foto(cog):
    persistencia.guardar({1: _foto_guardada()})
    guild = FakeGuild(1)
    guild.voice_client = None

    await cog.teardown(guild)

    assert _en_disco() == {}


async def test_si_el_guardador_fotografia_mientras_se_desconecta_la_foto_igual_se_borra(jugadores, cog):
    cog._persistencia = "activa"
    vc = jugadores.nuevo()
    cog._guardar(siempre=False)
    assert list(_en_disco()) == [1]
    desconectar = vc.disconnect

    async def _desconectar_con_guardado_en_medio():
        cog._guardar(siempre=True)
        await desconectar()

    vc.disconnect = _desconectar_con_guardado_en_medio
    await cog.teardown(vc.guild)

    assert _en_disco() == {}


# --- guardar_y_cerrar --------------------------------------------------------------------------

async def test_guardar_y_cerrar_escribe_la_foto_final_aunque_nada_haya_cambiado(jugadores, cog, escrituras):
    vc = jugadores.nuevo()
    cog._persistencia = "activa"
    cog._guardar(siempre=False)
    vc._last_update, vc._inicio_ns, vc._last_position = 2, 1, 61_000

    await cog.guardar_y_cerrar()

    assert len(escrituras) == 2
    assert _en_disco()[1]["posicion"] == 61_000


async def test_guardar_y_cerrar_frena_la_tarea_y_ya_no_se_escribe(jugadores, cog, ticks, escrituras):
    vc = jugadores.nuevo()
    cog._persistencia = "activa"
    guardador = cog._guardador = cog._segundo_plano(cog._guardar_en_bucle())
    await ticks(0)

    await cog.guardar_y_cerrar()

    assert guardador.cancelled()
    assert cog._guardador is None
    assert cog._persistencia == "cerrada"
    escritas = len(escrituras)
    # Sin players, otro tick guardaría una cola vacía encima de la buena.
    vc.connected = False
    await _correr_ticks(cog, ticks)
    assert len(escrituras) == escritas
    assert list(_en_disco()) == [1]


async def test_guardar_y_cerrar_dos_veces_no_escribe_la_segunda(jugadores, cog, escrituras):
    jugadores.nuevo()
    cog._persistencia = "activa"

    await cog.guardar_y_cerrar()
    await cog.guardar_y_cerrar()

    assert len(escrituras) == 1


async def test_guardar_y_cerrar_antes_de_retomar_no_pisa_lo_guardado(cog):
    persistencia.guardar({7: _foto_guardada()})

    await cog.guardar_y_cerrar()

    assert list(_en_disco()) == [7]
    assert cog._persistencia == "cerrada"


async def test_guardar_y_cerrar_con_el_disco_roto_no_levanta_nada(jugadores, cog, monkeypatch, caplog):
    jugadores.nuevo()
    cog._persistencia = "activa"

    def _falla(*args, **kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr(persistencia, "guardar", _falla)
    with caplog.at_level("ERROR", logger="music"):
        await cog.guardar_y_cerrar()

    assert cog._persistencia == "cerrada"
    assert "No pude guardar la foto final" in caplog.text


async def test_cerrar_a_mitad_de_retomar_no_reactiva_el_guardado(cog):
    persistencia.guardar({7: _foto_guardada()})

    class BotQueCierra(DiscordFalso):
        async def wait_until_ready(self):
            await cog.guardar_y_cerrar()

    cog.bot = BotQueCierra()
    await cog.retomar_guardadas()

    assert cog._persistencia == "cerrada"


# --- retomar_guardadas -------------------------------------------------------------------------

async def test_retomar_conecta_carga_la_cola_y_toca_desde_la_posicion(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada()})

    await cog.retomar_guardadas()

    canal, texto = guild.canales[10], guild.canales[20]
    vc = canal.player
    assert discord_falso.listo
    assert canal.clase is music_mod.resolver.ResolvingPlayer
    assert vc.resolver is cog.resolver
    assert vc.text_channel is texto
    assert [p.title for p in vc.queue] == ["uno", "dos"]
    [(actual, kwargs)] = vc.llamadas
    assert actual.title == "sonando"
    assert kwargs == {"start": 95_000, "paused": True, "add_history": False}
    assert vc.volume == 55
    assert vc.queue.mode is wavelink.QueueMode.loop
    assert cog.autoplay_activo(1) is False
    assert vc.autoplay is wavelink.AutoPlayMode.partial
    assert texto.enviados == [music_mod.AVISO_RETOMADA]
    assert cog._persistencia == "activa"


async def test_retomar_respeta_la_lista_blanca(cog, discord_falso, monkeypatch):
    uno, dos = discord_falso.servidor(1), discord_falso.servidor(2)
    persistencia.guardar({1: _foto_guardada(), 2: _foto_guardada()})
    monkeypatch.setattr(music_mod.config, "SERVIDORES_PERMITIDOS", frozenset({2}))

    await cog.retomar_guardadas()

    assert uno.canales[10].conexiones == 0
    assert dos.canales[10].conexiones == 1


async def test_retomar_con_la_lista_blanca_vacia_acepta_cualquier_servidor(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada()})

    await cog.retomar_guardadas()

    assert guild.canales[10].conexiones == 1


async def test_retomar_no_entra_a_un_canal_sin_humanos(cog, discord_falso, caplog):
    guild = discord_falso.servidor(1, humanos=0)
    persistencia.guardar({1: _foto_guardada()})

    with caplog.at_level("INFO", logger="music"):
        await cog.retomar_guardadas()

    assert guild.canales[10].conexiones == 0
    assert "no hay nadie en el canal" in caplog.text


async def test_retomar_no_cuenta_a_los_bots_como_gente(cog, discord_falso):
    guild = discord_falso.servidor(1)
    guild.canales[10].members = [SimpleNamespace(bot=True), SimpleNamespace(bot=True)]
    persistencia.guardar({1: _foto_guardada()})

    await cog.retomar_guardadas()

    assert guild.canales[10].conexiones == 0


async def test_retomar_no_toca_una_foto_vencida(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada(hora=time.time() - persistencia.VIGENCIA - 60)})

    await cog.retomar_guardadas()

    assert guild.canales[10].conexiones == 0


async def test_retomar_se_salta_el_servidor_donde_el_bot_ya_no_esta(cog, discord_falso):
    otro = discord_falso.servidor(2)
    persistencia.guardar({1: _foto_guardada(), 2: _foto_guardada()})

    await cog.retomar_guardadas()

    assert otro.canales[10].conexiones == 1


async def test_retomar_se_salta_el_canal_que_ya_no_existe(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada(voz=999)})

    await cog.retomar_guardadas()

    assert guild.canales[10].conexiones == 0


async def test_retomar_no_pisa_una_transmision_que_ya_existe(cog, discord_falso):
    guild = discord_falso.servidor(1)
    guild.voice_client = object()
    persistencia.guardar({1: _foto_guardada()})

    await cog.retomar_guardadas()

    assert guild.canales[10].conexiones == 0


async def test_un_servidor_que_falla_al_conectar_no_frena_a_los_otros(cog, discord_falso, caplog):
    roto, bueno = discord_falso.servidor(1), discord_falso.servidor(2)
    roto.canales[10].falla_al_conectar = True
    persistencia.guardar({1: _foto_guardada(), 2: _foto_guardada()})

    with caplog.at_level("ERROR", logger="music"):
        await cog.retomar_guardadas()

    assert bueno.canales[10].player.llamadas
    assert "No pude retomar la cola de 1" in caplog.text
    assert cog._persistencia == "activa"


async def test_un_servidor_que_falla_al_tocar_se_corta_y_no_frena_a_los_otros(cog, discord_falso, caplog, monkeypatch):
    roto, bueno = discord_falso.servidor(1), discord_falso.servidor(2)
    conectar = CanalDeVozRetomable.connect

    async def _conectar_roto(self, *, cls):
        vc = await conectar(self, cls=cls)
        vc.falla_el_play = self.guild.id == 1
        return vc

    monkeypatch.setattr(CanalDeVozRetomable, "connect", _conectar_roto)
    persistencia.guardar({1: _foto_guardada(), 2: _foto_guardada()})

    with caplog.at_level("ERROR", logger="music"):
        await cog.retomar_guardadas()

    assert not roto.canales[10].player.connected
    assert bueno.canales[10].player.llamadas
    assert "No pude retomar la cola de 1" in caplog.text


async def test_retomar_solo_corre_una_vez(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada()})

    await cog.retomar_guardadas()
    guild.voice_client = None
    await cog.retomar_guardadas()

    assert guild.canales[10].conexiones == 1


async def test_sin_nada_guardado_retomar_deja_el_guardado_activo(cog, discord_falso):
    await cog.retomar_guardadas()

    assert cog._persistencia == "activa"


async def test_con_el_archivo_roto_retomar_no_levanta_nada(cog, discord_falso, archivo_de_colas):
    archivo_de_colas.parent.mkdir(parents=True)
    archivo_de_colas.write_text("{roto")

    await cog.retomar_guardadas()

    assert cog._persistencia == "activa"


async def test_sin_tema_en_curso_empieza_por_la_cola(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada(actual=None, posicion=0)})

    await cog.retomar_guardadas()

    vc = guild.canales[10].player
    [(primero, kwargs)] = vc.llamadas
    assert primero.title == "uno"
    assert kwargs == {}
    assert [p.title for p in vc.queue] == ["dos"]


async def test_una_pista_guardada_que_no_se_puede_rearmar_se_salta(cog, discord_falso, caplog):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada(cola=[{"encoded": "roto"}, _raw("buena")])})

    with caplog.at_level("WARNING", logger="music"):
        await cog.retomar_guardadas()

    assert [p.title for p in guild.canales[10].player.queue] == ["buena"]
    assert "no se puede rearmar" in caplog.text


async def test_un_modo_de_cola_desconocido_vuelve_a_normal(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada(modo="de_otra_version")})

    await cog.retomar_guardadas()

    assert guild.canales[10].player.queue.mode is wavelink.QueueMode.normal


async def test_si_no_queda_nada_que_tocar_se_corta_en_vez_de_quedarse_mudo(cog, discord_falso, caplog):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada(actual={"encoded": "roto"}, cola=[])})

    with caplog.at_level("ERROR", logger="music"):
        await cog.retomar_guardadas()

    assert not guild.canales[10].player.connected
    assert "no quedó nada que tocar" in caplog.text


async def test_sin_canal_de_texto_retoma_igual_y_no_avisa_a_nadie(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada(texto=None)})

    await cog.retomar_guardadas()

    vc = guild.canales[10].player
    assert vc.text_channel is None
    assert vc.llamadas
    assert guild.canales[20].enviados == []


async def test_si_el_aviso_no_sale_la_cola_sigue_sonando(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada()})

    async def _prohibido(contenido=None, **kwargs):
        raise music_mod.discord.Forbidden(SimpleNamespace(status=403, reason="x"), "sin permiso")

    guild.canales[20].send = _prohibido
    await cog.retomar_guardadas()

    vc = guild.canales[10].player
    assert vc.connected
    assert vc.llamadas


# --- saltar las partes que no son música -----------------------------------------------

class Escenario:
    """Un tema sonando con reloj, posición y reportes de Lavalink simulados (uno cada 5 s, solo mientras suena)."""

    def __init__(self, monkeypatch, vc):
        self.vc, self.ns, self.t, self.pos, self.reporte, self.eventos = vc, 10**12, 0.0, 0, 0.0, []
        vc.escenario = self
        original = asyncio.sleep
        monkeypatch.setattr(music_mod, "time", SimpleNamespace(monotonic_ns=lambda: self.ns, monotonic=time.monotonic))

        async def dormir(segundos, *a):
            self.avanzar(segundos)
            await original(0)

        monkeypatch.setattr(music_mod.asyncio, "sleep", dormir)

    def a_los(self, segundos, accion):
        """Algo que pasa cuando el reloj llega a ese segundo: una pausa, un seek del usuario…"""
        self.eventos = sorted([*self.eventos, (segundos, accion)], key=lambda e: e[0])

    def avanzar(self, segundos):
        while segundos > 0:
            paso = min(segundos, 0.5)
            segundos -= paso
            self.ns += int(paso * 1e9)
            self.t += paso
            if not self.vc.paused:
                self.pos += int(paso * 1000)
                if self.t - self.reporte >= 5:
                    self.reporte = self.t
                    self.vc._last_update, self.vc._last_position = self.ns, self.pos
            while self.eventos and self.eventos[0][0] <= self.t:
                self.eventos.pop(0)[1]()


class PlayerQueSalta(FakePlayer):
    """Un player donde se ven los seeks y los skips, cada uno con la posición real del momento."""

    def __init__(self, pista):
        super().__init__(playing=True)
        self.current = pista
        self.text_channel = FakeCanalTexto()
        self.seeks, self.skips = [], []
        self.falla_el_seek = False

    async def seek(self, ms):
        if self.falla_el_seek:
            raise RuntimeError("Lavalink dijo que no")
        self.seeks.append((ms, self.escenario.pos))
        self.escenario.pos = ms

    async def skip(self, force=True):
        self.skips.append(self.escenario.pos)


def _tema(id_="abc", largo=200_000):
    tema = FakeTrack(id_)
    tema.length = largo
    tema.source = "youtube"
    tema.uri = f"https://www.youtube.com/watch?v={id_}"
    return tema


async def _arrancar(cog, monkeypatch, segmentos, *, arranque=0, pista=None):
    """Arranca un tema de 200 s con esos segmentos ya en la caché y devuelve (player, escenario, estación, pista)."""
    pista = pista or _tema()
    vc = PlayerQueSalta(pista)
    sb = SponsorBlock(activo=True)
    sb._cache["abc"] = tuple(segmentos)
    vc.resolver = Resolver(sponsorblock=sb)
    vc.arranque_ms = arranque
    escenario = Escenario(monkeypatch, vc)
    escenario.pos = arranque
    await cog.on_wavelink_track_start(FakePayloadInicio(vc, pista))
    return vc, escenario, cog.estacion(vc.guild.id), pista


async def _terminar(est):
    """Deja correr los vigías hasta que acaben, con tope por si alguno se queda dando vueltas."""
    await asyncio.wait_for(asyncio.gather(*list(est.segmentos), return_exceptions=True), timeout=5)


def _lineas_de_salto(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "music" and "no es música" in r.getMessage()]


async def test_una_parte_del_medio_se_salta_con_seek_a_su_final(cog, sin_panel, monkeypatch, caplog):
    with caplog.at_level("INFO", logger="music"):
        vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(60_000, 75_000, 200_000)])
        assert len(est.segmentos) == 1
        await _terminar(est)

    [(destino, donde)] = vc.seeks
    assert destino == 75_000 and 60_000 <= donde <= 62_500
    assert vc.skips == [] and est.segmentos == []
    [linea] = _lineas_de_salto(caplog)
    assert "abc" in linea and "60" in linea and "75" in linea


@pytest.mark.parametrize("fin", [195_000, 198_000, 200_000])
async def test_la_parte_que_llega_al_final_salta_al_tema_siguiente(cog, sin_panel, monkeypatch, caplog, fin):
    with caplog.at_level("INFO", logger="music"):
        vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(170_000, fin, 200_000)])
        await _terminar(est)

    [donde] = vc.skips
    assert 170_000 <= donde <= 172_500
    assert vc.seeks == []
    assert len(_lineas_de_salto(caplog)) == 1


async def test_una_parte_que_termina_lejos_del_final_no_salta_de_tema(cog, sin_panel, monkeypatch):
    vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(170_000, 190_000, 200_000)])

    await _terminar(est)

    assert [d for d, _ in vc.seeks] == [190_000] and vc.skips == []


async def test_cada_parte_tiene_su_vigia_y_se_saltan_en_orden(cog, sin_panel, monkeypatch):
    partes = [Segmento(30_000, 40_000, 200_000), Segmento(100_000, 110_000, 200_000), Segmento(180_000, 200_000, 200_000)]
    vc, _, est, _ = await _arrancar(cog, monkeypatch, partes)
    assert len(est.segmentos) == 3

    await _terminar(est)

    assert [d for d, _ in vc.seeks] == [40_000, 110_000]
    assert len(vc.skips) == 1 and 180_000 <= vc.skips[0] <= 186_000
    assert est.segmentos == []


async def test_una_pausa_del_usuario_corre_los_tiempos(cog, sin_panel, monkeypatch):
    """Pausa de 30 s a los 50: dormir 60 s desde el arranque saltaría 10 s de canción."""
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)])
    escenario.a_los(50, lambda: setattr(vc, "paused", True))
    escenario.a_los(80, lambda: setattr(vc, "paused", False))

    await _terminar(est)

    [(destino, donde)] = vc.seeks
    assert destino == 70_000 and 60_000 <= donde <= 62_500
    assert escenario.t >= 89


async def test_un_seek_del_usuario_hacia_adelante_se_nota_y_nunca_se_vuelve_atras(cog, sin_panel, monkeypatch):
    partes = [Segmento(60_000, 70_000, 200_000), Segmento(100_000, 110_000, 200_000)]
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, partes)
    escenario.a_los(10, lambda: setattr(escenario, "pos", 95_000))

    await _terminar(est)

    [(destino, donde)] = vc.seeks
    assert destino == 110_000 and 100_000 <= donde <= 107_000


@pytest.mark.parametrize("pasa", ["otra pista", "desconectado"])
async def test_no_actua_si_la_pista_cambio_o_el_player_se_fue(cog, sin_panel, monkeypatch, pasa):
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)])
    if pasa == "otra pista":
        escenario.a_los(20, lambda: setattr(vc, "current", _tema("otra")))
    else:
        escenario.a_los(20, lambda: setattr(vc, "connected", False))

    await _terminar(est)

    assert vc.seeks == [] and vc.skips == []
    assert est.segmentos == []


async def test_un_reintento_que_retoma_despues_de_la_parte_no_vuelve_atras(cog, sin_panel, monkeypatch):
    vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)], arranque=80_000)

    await _terminar(est)

    assert vc.seeks == [] and vc.skips == []


async def test_la_intro_ya_saltada_al_arrancar_no_se_salta_otra_vez(cog, sin_panel, monkeypatch):
    vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(0, 12_500, 200_000)], arranque=12_500)

    await _terminar(est)

    assert vc.seeks == []


async def test_una_intro_que_llego_tarde_se_salta_con_seek(cog, sin_panel, monkeypatch):
    """Si las marcas llegaron después de `play()`, el vigía recupera la intro."""
    vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(0, 12_500, 200_000)])

    await _terminar(est)

    [(destino, donde)] = vc.seeks
    assert destino == 12_500 and donde <= 2_500


async def test_si_el_seek_falla_se_avisa_y_no_escapa_ninguna_excepcion(cog, sin_panel, monkeypatch, caplog):
    vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)])
    vc.falla_el_seek = True
    tareas = list(est.segmentos)

    with caplog.at_level("WARNING", logger="music"):
        resultados = await asyncio.wait_for(asyncio.gather(*tareas, return_exceptions=True), timeout=5)

    assert resultados == [None]
    assert "No pude saltar" in caplog.text


async def test_al_terminar_la_cancion_se_cancelan_los_vigias(cog, sin_panel, monkeypatch):
    vc, _, est, pista = await _arrancar(cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)])
    vc.queue.append(FakeTrack("la que sigue"))
    tareas = list(est.segmentos)

    await cog.on_wavelink_track_end(FakePayloadFin(vc, pista, reason="replaced"))
    await asyncio.gather(*tareas, return_exceptions=True)

    assert all(t.cancelled() for t in tareas)
    assert est.segmentos == [] and vc.seeks == []


async def test_el_final_de_un_servidor_sin_estacion_no_la_crea(cog):
    vc = FakePlayer()
    vc.queue.append(FakeTrack("la que sigue"))

    await cog.on_wavelink_track_end(FakePayloadFin(vc, FakeTrack("la que terminó"), reason="replaced"))

    assert cog._estaciones == {}


async def test_el_vigia_que_ya_esta_actuando_no_se_cancela_con_el_final_que_provoca(cog, sin_panel, monkeypatch):
    """El skip de un vigía dispara el `track_end` del tema: no puede cortarle el skip a medias."""
    vc, _, est, pista = await _arrancar(cog, monkeypatch, [Segmento(180_000, 200_000, 200_000)])
    vc.queue.append(FakeTrack("la que sigue"))
    [vigia] = est.segmentos
    skip = vc.skip

    async def _skip_con_final(force=True):
        await skip(force)
        await cog.on_wavelink_track_end(FakePayloadFin(vc, pista, reason="replaced"))

    vc.skip = _skip_con_final
    await asyncio.wait_for(vigia, timeout=5)

    assert not vigia.cancelled() and len(vc.skips) == 1


async def test_un_corte_del_dj_no_programa_saltos(cog, sin_panel, monkeypatch):
    clip = _tema()
    clip.is_dj_clip = True

    _, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)], pista=clip)

    assert est.segmentos == []


async def test_un_tema_sin_marcas_no_programa_nada(cog, sin_panel, monkeypatch):
    _, _, est, _ = await _arrancar(cog, monkeypatch, [])

    assert est.segmentos == []


async def test_con_sponsorblock_apagado_no_se_programa_ni_se_consulta_nada(cog, sin_panel):
    pista = _tema()
    vc = PlayerQueSalta(pista)
    vc.resolver = Resolver()
    assert vc.resolver.sponsorblock.activo is False

    await cog.on_wavelink_track_start(FakePayloadInicio(vc, pista))

    assert cog.estacion(1).segmentos == []
    assert vc.resolver.sponsorblock._http is None and vc.resolver.sponsorblock._en_curso == {}


async def test_si_el_resolver_no_sabe_de_segmentos_el_panel_sale_igual(cog, monkeypatch, caplog):
    publicados = []

    async def _panel(self, guild, vc):
        publicados.append(vc)

    monkeypatch.setattr(Music, "_post_panel", _panel)
    pista = _tema()
    vc = PlayerQueSalta(pista)
    vc.resolver = ResolverQueAnota()

    with caplog.at_level("WARNING", logger="music"):
        await cog.on_wavelink_track_start(FakePayloadInicio(vc, pista))

    assert publicados == [vc]
    assert cog.estacion(1).segmentos == []
    assert "partes que no son música" in caplog.text


async def test_al_descargar_el_cog_se_cierra_el_resolver(cog):
    cerrado = []

    async def _cerrar():
        cerrado.append(True)

    cog.resolver.cerrar = _cerrar
    await cog.cog_unload()

    assert cerrado == [True]


@pytest.mark.parametrize("segundos_desde_el_reporte, pausado, esperado", [
    (3, False, 53_000),
    (6, False, 56_000),
    (7, False, 50_000),
    (300, False, 50_000),
    (3, True, 50_000),
])
def test_la_posicion_solo_se_extrapola_de_un_reporte_reciente_y_sin_pausa(
    monkeypatch, segundos_desde_el_reporte, pausado, esperado
):
    """Tras una pausa larga el reporte viejo adelantaría la cuenta y se saltaría música."""
    reporte = 5 * 10**9
    ahora = reporte + segundos_desde_el_reporte * 10**9
    monkeypatch.setattr(music_mod, "time", SimpleNamespace(monotonic_ns=lambda: ahora))
    vc = SimpleNamespace(_last_update=reporte, _last_position=50_000, _inicio_ns=10**9, paused=pausado)

    assert Music.posicion_viva(vc) == esperado


def test_antes_del_primer_reporte_la_posicion_cuenta_desde_donde_arranco_el_tema(monkeypatch):
    monkeypatch.setattr(music_mod, "time", SimpleNamespace(monotonic_ns=lambda: 4 * 10**9))
    vc = SimpleNamespace(_last_update=None, _inicio_ns=10**9, arranque_ms=12_500, paused=False)

    assert Music.posicion_viva(vc) == 15_500


def test_un_reporte_del_tema_anterior_no_cuenta(monkeypatch):
    monkeypatch.setattr(music_mod, "time", SimpleNamespace(monotonic_ns=lambda: 4 * 10**9))
    vc = SimpleNamespace(_last_update=5 * 10**8, _last_position=170_000, _inicio_ns=10**9, arranque_ms=0, paused=False)

    assert Music.posicion_viva(vc) == 3_000


# --- comandos híbridos: `!` y `/` ------------------------------------------------

HIBRIDOS = {
    "play", "playnext", "autoplay", "pause", "resume", "skip", "stop", "queue", "nowplaying",
    "panel", "loop", "shuffle", "volume", "remove", "dj", "lyrics", "letra",
}
CON_CONTROL = ("pause", "resume", "skip", "stop", "loop", "shuffle", "volume", "remove", "letra")
ALIAS = {
    "p": "play", "pn": "playnext", "next": "playnext", "ap": "autoplay", "r": "resume", "s": "skip",
    "q": "queue", "np": "nowplaying", "l": "loop", "vol": "volume", "rm": "remove", "ly": "lyrics",
    "buscar": "search",
}


@pytest.fixture
async def bot_con_musica():
    """Un `Bot` de verdad, sin conectarse, con el cog cargado: así se ve el cog como lo ve discord.py."""
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    await bot.add_cog(Music(bot))
    yield bot
    await bot.remove_cog("Music")


def _ctx_de_barra(bot, vc=None, author=None):
    """Un contexto de mentira con el `interaction` que hace que discord.py corra los checks por el camino de `/`."""
    ctx = FakeCtx(vc, author=author)
    ctx.interaction = SimpleNamespace(client=bot)
    ctx.interaction._baton = ctx
    return ctx


def test_los_comandos_de_musica_son_hibridos_menos_search():
    comandos = {c.name: c for c in Music.__cog_commands__}

    assert {n for n, c in comandos.items() if isinstance(c, commands.HybridCommand)} == HIBRIDOS
    # Elige con reacciones sobre un mensaje y `/play` ya autocompleta: queda solo con prefijo.
    assert set(comandos) == HIBRIDOS | {"search"}


async def test_los_alias_valen_con_prefijo_y_el_arbol_de_barra_solo_trae_los_nombres(bot_con_musica):
    for alias, nombre in ALIAS.items():
        assert bot_con_musica.get_command(alias).name == nombre

    assert {c.name for c in bot_con_musica.tree.get_commands()} == HIBRIDOS


def test_nombres_y_descripciones_respetan_los_limites_de_discord():
    nombre_valido = re.compile(r"[a-z0-9_-]{1,32}")
    for comando in Music.__cog_commands__:
        if comando.name not in HIBRIDOS:
            continue
        app = comando.app_command
        assert nombre_valido.fullmatch(app.name), app.name
        assert 1 <= len(app.description) <= 100 and app.description != "…", app.name
        for parametro in app.parameters:
            assert nombre_valido.fullmatch(parametro.name), (app.name, parametro.name)
            assert 1 <= len(parametro.description) <= 100 and parametro.description != "…", (app.name, parametro.name)
            assert all(len(c.name) <= 100 and len(str(c.value)) <= 100 for c in parametro.choices)


def test_solo_query_de_play_y_playnext_se_autocompleta():
    autocompletados = {
        (c.name, p.name) for c in Music.__cog_commands__ if c.name in HIBRIDOS
        for p in c.app_command.parameters if p.autocomplete
    }

    assert autocompletados == {("play", "query"), ("playnext", "query")}


# --- `defer`: Discord da 3 s para contestar a una interacción ---------------------

@pytest.mark.parametrize("comando, args", [
    ("play", {"query": "algo"}), ("playnext", {"query": "algo"}), ("lyrics_cmd", {}), ("stop", {}), ("panel_cmd", {}),
])
async def test_los_comandos_lentos_difieren_la_respuesta_antes_de_hacer_nada(cog, monkeypatch, comando, args):
    ctx = FakeCtx()

    async def _sin_voz(self, ctx):
        ctx.eventos.append("voz")

    monkeypatch.setattr(Music, "_connect_voice", _sin_voz)

    await getattr(cog, comando)(cog, ctx, **args)

    assert ctx.eventos[0] == "defer"
    if comando in ("play", "playnext"):
        assert ctx.eventos == ["defer", "voz"]


async def test_defer_con_prefijo_no_hace_nada_y_con_barra_difiere_la_interaccion():
    """Lo que se da por hecho de discord.py: `Context.defer` es un no-op sin interacción."""
    pedidos = []

    async def _defer(*, ephemeral):
        pedidos.append(ephemeral)

    con_prefijo = commands.Context(message=SimpleNamespace(_state=None), bot=None, view=None)
    con_barra = commands.Context(message=SimpleNamespace(_state=None), bot=None, view=None)
    con_barra.interaction = SimpleNamespace(response=SimpleNamespace(defer=_defer))

    await con_prefijo.defer()
    assert pedidos == []

    await con_barra.defer(ephemeral=True)
    assert pedidos == [True]


@pytest.mark.parametrize("interaccion, avisa", [(object(), True), (None, False)])
async def test_con_barra_un_tema_que_arranca_directo_cierra_la_respuesta_diferida(cog, monkeypatch, interaccion, avisa):
    """Con prefijo el panel ya es el aviso; con `/` la interacción en "pensando…" tiene que terminar en un mensaje."""
    monkeypatch.setattr(music_mod.wavelink.Playable, "search", buscador([FakeTrack("única")]))
    for comando in ("play", "playnext"):
        vc = FakePlayer(playing=False)
        ctx = FakeCtx(vc, interaction=interaccion)
        monkeypatch.setattr(Music, "_connect_voice", lambda self, ctx, vc=vc: _listo(vc))

        await getattr(cog, comando)(cog, ctx, query="única")

        assert [t.title for t in vc.tocando] == ["única"]
        assert ctx.enviados == (["▶️ **única**"] if avisa else [])


@pytest.mark.parametrize("interaccion, avisa", [(object(), True), (None, False)])
async def test_con_barra_el_panel_responde_aunque_se_publique_en_el_canal(cog, monkeypatch, interaccion, avisa):
    publicados = []

    async def _panel(self, guild, vc):
        publicados.append(vc)

    monkeypatch.setattr(Music, "_post_panel", _panel)
    vc = FakePlayer(playing=True)
    vc.current = FakeTrack()
    ctx = FakeCtx(vc, interaction=interaccion)

    await cog.panel_cmd(cog, ctx)

    assert publicados == [vc]
    assert ctx.enviados == (["Panel publicado."] if avisa else [])


async def test_dj_acepta_volumen_como_alias_de_vol(cog):
    ctx = FakeCtx()

    await cog.dj_cmd(cog, ctx, "volumen", "50")

    assert cog.dj.volume == 50


# --- los checks también corren con `/` --------------------------------------------

async def test_con_barra_el_cog_check_sigue_negando_los_mensajes_directos(bot_con_musica):
    ctx = _ctx_de_barra(bot_con_musica)
    ctx.guild = None

    for nombre in ("play", "queue", "skip"):
        with pytest.raises(commands.NoPrivateMessage):
            await bot_con_musica.get_command(nombre).can_run(ctx)


async def test_con_barra_los_controles_siguen_siendo_de_quien_escucha(bot_con_musica):
    vc = FakePlayer()
    afuera = _ctx_de_barra(bot_con_musica, vc, FakeAuthor(channel="otro-canal"))
    adentro = _ctx_de_barra(bot_con_musica, vc, FakeAuthor(channel="canal-de-voz"))

    for nombre in CON_CONTROL:
        comando = bot_con_musica.get_command(nombre)
        with pytest.raises(commands.CheckFailure, match="mismo canal"):
            await comando.can_run(afuera)
        assert await comando.can_run(adentro)


# --- autocompletado de /play y /playnext ------------------------------------------

class FakeResultado:
    """Lo que el autocompletado lee de cada tema que devuelve la búsqueda."""

    def __init__(self, titulo="Never Gonna Give You Up", autor="Rick Astley", largo=225_000,
                 uri="https://www.youtube.com/watch?v=dQw4w9WgXcQ", en_vivo=False):
        self.title, self.author, self.length, self.uri, self.is_stream = titulo, autor, largo, uri, en_vivo


def _buscador_que_anota(monkeypatch, resultado):
    """Cambia `Playable.search` y devuelve la lista donde anota cada consulta."""
    consultas = []

    async def _search(query, *a, **kw):
        consultas.append(query)
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    monkeypatch.setattr(music_mod.wavelink.Playable, "search", _search)
    return consultas


@pytest.mark.parametrize("texto", ["", "  ", "a", "ab", "  ab  "])
async def test_con_menos_de_tres_caracteres_no_se_busca_ni_se_sugiere(cog, monkeypatch, texto):
    consultas = _buscador_que_anota(monkeypatch, [FakeResultado()])

    assert await cog.buscar_sugerencias(texto) == []
    assert consultas == []


async def test_una_url_se_ofrece_tal_cual_sin_buscar(cog, monkeypatch):
    consultas = _buscador_que_anota(monkeypatch, [FakeResultado()])
    url = "https://www.youtube.com/watch?v=abc"

    opciones = await cog.buscar_sugerencias(f"  {url} ")

    assert [(o.name, o.value) for o in opciones] == [(url, url)]
    assert consultas == []


async def test_una_url_que_no_cabe_en_una_opcion_no_ofrece_nada(cog, monkeypatch):
    consultas = _buscador_que_anota(monkeypatch, [FakeResultado()])

    assert await cog.buscar_sugerencias("https://www.youtube.com/playlist?list=" + "a" * 100) == []
    assert consultas == []


async def test_cada_opcion_es_artista_titulo_y_duracion_con_la_url_como_valor(cog, monkeypatch):
    consultas = _buscador_que_anota(monkeypatch, [FakeResultado(), FakeResultado("Otro", "Alguien", 61_000, "https://youtu.be/x")])

    opciones = await cog.buscar_sugerencias("never gonna")

    assert [(o.name, o.value) for o in opciones] == [
        ("Rick Astley – Never Gonna Give You Up (3:45)", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("Alguien – Otro (1:01)", "https://youtu.be/x"),
    ]
    assert consultas == ["never gonna"]


async def test_un_nombre_largo_se_recorta_a_100_sin_perder_la_duracion(cog, monkeypatch):
    _buscador_que_anota(monkeypatch, [FakeResultado(titulo="T" * 300)])

    (opcion,) = await cog.buscar_sugerencias("titulo largo")

    assert len(opcion.name) == 100
    assert opcion.name.startswith("Rick Astley – TTT") and opcion.name.endswith("… (3:45)")


@pytest.mark.parametrize("uri", [None, "", "https://www.youtube.com/watch?v=" + "a" * 100])
async def test_si_la_url_no_existe_o_no_cabe_el_valor_es_el_titulo(cog, monkeypatch, uri):
    _buscador_que_anota(monkeypatch, [FakeResultado(uri=uri)])

    (opcion,) = await cog.buscar_sugerencias("never gonna")

    assert opcion.value == "Never Gonna Give You Up"


async def test_el_valor_nunca_pasa_de_100_aunque_el_titulo_sea_enorme(cog, monkeypatch):
    _buscador_que_anota(monkeypatch, [FakeResultado(titulo="T" * 300, uri=None)])

    (opcion,) = await cog.buscar_sugerencias("titulo largo")

    assert opcion.value == "T" * 100


async def test_sin_artista_el_nombre_es_el_titulo_y_un_directo_no_lleva_duracion(cog, monkeypatch):
    _buscador_que_anota(monkeypatch, [FakeResultado("Radio", "", 9_000_000_000, en_vivo=True), FakeResultado("Sin largo", "X", 0)])

    opciones = await cog.buscar_sugerencias("radio")

    assert [o.name for o in opciones] == ["Radio (en vivo)", "X – Sin largo"]


async def test_hay_como_mucho_25_opciones_y_se_saltan_los_temas_sin_titulo(cog, monkeypatch):
    resultados = [FakeResultado(titulo="")] + [FakeResultado(titulo=f"Tema {i}") for i in range(40)]
    _buscador_que_anota(monkeypatch, resultados)

    opciones = await cog.buscar_sugerencias("tema")

    assert len(opciones) == 25
    assert opciones[0].name.startswith("Rick Astley – Tema 0")


async def test_una_playlist_no_se_sugiere(cog, monkeypatch):
    lista = wavelink.Playlist.__new__(wavelink.Playlist)
    lista.name, lista.tracks = "Una lista", [FakeResultado()]
    _buscador_que_anota(monkeypatch, lista)

    assert await cog.buscar_sugerencias("una lista") == []


@pytest.fixture
def reloj_de_sugerencias(monkeypatch):
    """`time.monotonic` del cog, que solo avanza cuando uno lo empuja."""
    marca = {"t": 1000.0}
    monkeypatch.setattr(music_mod, "time", SimpleNamespace(monotonic=lambda: marca["t"]))

    def avanzar(segundos):
        marca["t"] += segundos

    return avanzar


async def test_la_misma_busqueda_sale_de_la_cache_hasta_que_vence(cog, monkeypatch, reloj_de_sugerencias):
    consultas = _buscador_que_anota(monkeypatch, [FakeResultado()])

    primera = await cog.buscar_sugerencias("Never Gonna")
    reloj_de_sugerencias(music_mod.SUGERENCIAS_VIGENCIA - 1)
    otra = await cog.buscar_sugerencias("  never gonna ")

    assert consultas == ["Never Gonna"]
    assert otra == primera

    reloj_de_sugerencias(2)
    await cog.buscar_sugerencias("never gonna")

    assert consultas == ["Never Gonna", "never gonna"]


async def test_una_busqueda_sin_resultados_no_se_guarda(cog, monkeypatch, reloj_de_sugerencias):
    consultas = _buscador_que_anota(monkeypatch, [])

    await cog.buscar_sugerencias("nada de nada")
    await cog.buscar_sugerencias("nada de nada")

    assert len(consultas) == 2 and cog._sugerencias == {}


async def test_la_cache_tiene_tope_y_suelta_primero_lo_mas_viejo(cog, monkeypatch, reloj_de_sugerencias):
    monkeypatch.setattr(music_mod, "SUGERENCIAS_EN_CACHE", 2)
    consultas = _buscador_que_anota(monkeypatch, [FakeResultado()])

    for texto in ("uno", "dos", "tres"):
        await cog.buscar_sugerencias(texto)

    assert list(cog._sugerencias) == ["dos", "tres"]
    await cog.buscar_sugerencias("uno")
    assert consultas == ["uno", "dos", "tres", "uno"]


async def test_lo_vencido_se_limpia_al_guardar_algo_nuevo(cog, monkeypatch, reloj_de_sugerencias):
    _buscador_que_anota(monkeypatch, [FakeResultado()])
    await cog.buscar_sugerencias("viejo")

    reloj_de_sugerencias(music_mod.SUGERENCIAS_VIGENCIA + 1)
    await cog.buscar_sugerencias("nuevo")

    assert list(cog._sugerencias) == ["nuevo"]


async def test_si_lavalink_tarda_mas_del_plazo_no_hay_opciones(cog, monkeypatch):
    async def _lenta(query, *a, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(music_mod.wavelink.Playable, "search", _lenta)
    monkeypatch.setattr(music_mod, "SUGERENCIAS_ESPERA", 0.01)

    assert await asyncio.wait_for(cog.buscar_sugerencias("tema lento"), 1) == []
    assert cog._sugerencias == {}


@pytest.mark.parametrize("error", [wavelink.InvalidNodeException("sin nodo"), RuntimeError("se cayó")])
async def test_si_la_busqueda_falla_no_hay_opciones_y_nada_se_rompe(cog, monkeypatch, error):
    _buscador_que_anota(monkeypatch, error)

    assert await cog.buscar_sugerencias("tema roto") == []


async def test_el_autocompletado_de_barra_busca_en_un_servidor_permitido(cog, monkeypatch):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({7}))
    _buscador_que_anota(monkeypatch, [FakeResultado()])

    opciones = await cog.autocompletar_busqueda(SimpleNamespace(guild_id=7), "never gonna")

    assert [o.name for o in opciones] == ["Rick Astley – Never Gonna Give You Up (3:45)"]


@pytest.mark.parametrize("permitidos, guild_id", [(frozenset({7}), 8), (frozenset(), None), (frozenset({7}), None)])
async def test_el_autocompletado_no_busca_para_un_servidor_ajeno_ni_por_privado(cog, monkeypatch, permitidos, guild_id):
    """Discord no pasa el autocompletado por los checks: el de servidores tiene que repetirse aquí."""
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", permitidos)
    consultas = _buscador_que_anota(monkeypatch, [FakeResultado()])

    assert await cog.autocompletar_busqueda(SimpleNamespace(guild_id=guild_id), "never gonna") == []
    assert consultas == []


# --- la letra en vivo -----------------------------------------------------------------

LETRA_EN_VIVO = Letra(True, [(0, "uno"), (5000, "dos"), (9000, "tres")])


class MensajeDeLetra:
    def __init__(self, canal, contenido):
        self.canal, self.contenido, self.borrado = canal, contenido, False

    async def edit(self, *, content, **kwargs):
        self.contenido = content
        self.canal.registro.append(("edita", content))

    async def delete(self):
        self.borrado = True
        self.canal.registro.append(("borra", self.contenido))


class CanalDeLetra:
    """El canal de texto del player: lo que publica el panel y lo que hace la letra queda en un mismo registro."""

    def __init__(self, registro):
        self.registro, self.mensajes = registro, []

    async def send(self, contenido=None, **kwargs):
        self.mensajes.append(MensajeDeLetra(self, contenido))
        self.registro.append(("publica", contenido))
        return self.mensajes[-1]

    @property
    def vivos(self):
        return [m for m in self.mensajes if not m.borrado]


@pytest.fixture
async def en_vivo(cog, monkeypatch):
    """El cog con el panel y la búsqueda de letras de mentira, y la posición quieta en el segundo 0."""
    registro, busquedas = [], []

    async def _panel(self, guild, vc):
        registro.append("panel")

    async def _encontrar(session, titulo, artista, cancion, duracion_ms=0):
        busquedas.append((titulo, artista, cancion, duracion_ms))
        return en_vivo.letra

    async def _sin_desconexion(self, guild, vc):
        return None

    monkeypatch.setattr(Music, "_post_panel", _panel)
    monkeypatch.setattr(Music, "_schedule_idle_disconnect", _sin_desconexion)
    monkeypatch.setattr(music_mod.lyrics, "find_synced", _encontrar)
    cog.posicion_viva = lambda vc: vc.pos
    en_vivo = SimpleNamespace(registro=registro, busquedas=busquedas, letra=LETRA_EN_VIVO)
    yield en_vivo
    for est in cog._estaciones.values():
        est.letra.cancelar()


def _sonando(en_vivo, titulo="Tema A", con_canal=True):
    vc = FakePlayer(playing=True)
    vc.guild.voice_client = vc
    vc.current = FakeTrack(titulo)
    vc.pos = 0
    vc.text_channel = CanalDeLetra(en_vivo.registro) if con_canal else None
    return vc


async def _esperar(condicion, vueltas=1000):
    for _ in range(vueltas):
        if condicion():
            return
        await asyncio.sleep(0)
    raise AssertionError("no llegó a pasar lo esperado")


async def _arranca(cog, vc, pista=None):
    """Arranca un tema como lo hace Lavalink: `vc.current` ya es el tema cuando llega el evento."""
    vc.current = pista or vc.current
    await cog.on_wavelink_track_start(FakePayloadInicio(vc, vc.current))


async def _publicada(vc, cantidad=1):
    await _esperar(lambda: len(vc.text_channel.mensajes) >= cantidad)


@pytest.mark.parametrize("modo, esperado", [("", [True, False, True]), ("on", [True, True, True]), ("off", [False, False, False])])
async def test_letra_alterna_o_fija_el_estado_y_responde_en_privado(cog, en_vivo, modo, esperado):
    ctx = FakeCtx(_sonando(en_vivo))
    estados = []

    for _ in esperado:
        await cog.letra_cmd(cog, ctx, modo)
        estados.append(cog.estacion(1).letra.activa)

    assert estados == esperado
    assert ctx.opciones == [{"ephemeral": True}] * 3


async def test_letra_encendida_lo_dice_y_sigue_el_tema_desde_donde_va(cog, en_vivo):
    vc = _sonando(en_vivo)
    ctx = FakeCtx(vc)

    await cog.letra_cmd(cog, ctx, "on")
    await _publicada(vc)

    assert "encendida" in ctx.ultimo and cog.estacion(1).letra.tarea is not None
    assert vc.text_channel.mensajes[0].contenido == armar_texto("Tema A", LETRA_EN_VIVO.lineas, 0)


async def test_letra_apagada_borra_el_mensaje_y_corta_la_tarea(cog, en_vivo):
    vc = _sonando(en_vivo)
    ctx = FakeCtx(vc)
    await cog.letra_cmd(cog, ctx, "on")
    await _publicada(vc)
    tarea = cog.estacion(1).letra.tarea

    await cog.letra_cmd(cog, ctx, "off")

    est = cog.estacion(1)
    assert "apagada" in ctx.ultimo and not est.letra.activa and est.letra.mensaje is None
    assert vc.text_channel.vivos == []
    await asyncio.gather(tarea, return_exceptions=True)
    assert tarea.cancelled()


async def test_letra_encendida_otra_vez_vuelve_a_publicar_el_mensaje_al_final_del_chat(cog, en_vivo):
    vc = _sonando(en_vivo)
    ctx = FakeCtx(vc)
    await cog.letra_cmd(cog, ctx, "on")
    await _publicada(vc)

    await cog.letra_cmd(cog, ctx, "on")
    await _publicada(vc, 2)

    assert [m.borrado for m in vc.text_channel.mensajes] == [True, False]


@pytest.mark.parametrize("modo", ["", "on", "off"])
@pytest.mark.parametrize("falta", ["voz", "tema"])
async def test_letra_sin_el_bot_sonando_avisa_y_no_cambia_nada(cog, en_vivo, modo, falta):
    vc = _sonando(en_vivo)
    if falta == "voz":
        vc.guild.voice_client = None
    else:
        vc.current = None
    ctx = FakeCtx(vc)

    await cog.letra_cmd(cog, ctx, modo)

    assert ctx.ultimo == "No hay nada reproduciendo."
    assert not cog.estacion(1).letra.activa and cog.estacion(1).letra.tarea is None
    assert vc.text_channel.mensajes == []


async def test_letra_sin_canal_de_texto_no_se_enciende(cog, en_vivo):
    ctx = FakeCtx(_sonando(en_vivo, con_canal=False))

    await cog.letra_cmd(cog, ctx, "on")

    assert "canal de texto" in ctx.ultimo and not cog.estacion(1).letra.activa


@pytest.mark.parametrize("modo, activa", [("ON", True), ("Off", False)])
async def test_letra_no_distingue_mayusculas(cog, en_vivo, modo, activa):
    cog.estacion(1).letra.activa = not activa

    await cog.letra_cmd(cog, FakeCtx(_sonando(en_vivo)), modo)

    assert cog.estacion(1).letra.activa is activa


async def test_letra_con_un_argumento_raro_explica_el_uso_y_no_cambia_nada(cog, en_vivo):
    ctx = FakeCtx(_sonando(en_vivo))

    await cog.letra_cmd(cog, ctx, "quizas")

    assert "Uso:" in ctx.ultimo and not cog.estacion(1).letra.activa


async def test_letra_encendida_mientras_habla_el_dj_solo_avisa_que_habla(cog, en_vivo):
    vc = _sonando(en_vivo)
    vc.current = _clip_del_dj()
    vc.current.uri = f"{music_mod.tts.CACHE_DIR}/dj_abc.wav"

    await cog.letra_cmd(cog, FakeCtx(vc), "on")

    assert [m.contenido for m in vc.text_channel.mensajes] == [HABLA_EL_DJ]
    assert cog.estacion(1).letra.tarea is None and en_vivo.busquedas == []


async def test_sin_pista_no_se_pone_la_letra(cog, en_vivo):
    vc = _sonando(en_vivo)
    cog.estacion(1).letra.activa = True

    await cog._poner_letra(vc, None, es_clip=False)

    assert cog.estacion(1).letra.tarea is None


async def test_con_la_letra_prendida_cada_tema_la_publica_debajo_del_panel_y_borra_la_anterior(cog, en_vivo):
    vc = _sonando(en_vivo)
    cog.estacion(1).letra.activa = True

    await _arranca(cog, vc)
    await _publicada(vc)
    await _arranca(cog, vc, FakeTrack("Tema B"))
    await _publicada(vc, 2)

    texto_a, texto_b = (armar_texto(t, LETRA_EN_VIVO.lineas, 0) for t in ("Tema A", "Tema B"))
    assert en_vivo.registro == ["panel", ("publica", texto_a), ("borra", texto_a), "panel", ("publica", texto_b)]
    assert [m.contenido for m in vc.text_channel.vivos] == [texto_b]


async def test_con_la_letra_apagada_el_cambio_de_tema_no_publica_nada(cog, en_vivo):
    vc = _sonando(en_vivo)

    await _arranca(cog, vc)
    await _arranca(cog, vc, FakeTrack("Tema B"))
    await asyncio.sleep(0)

    assert en_vivo.registro == ["panel", "panel"] and vc.text_channel.mensajes == [] and en_vivo.busquedas == []


async def test_la_letra_se_busca_una_vez_y_queda_en_la_cache_que_comparte_con_la_api(cog, en_vivo):
    vc = _sonando(en_vivo, "Rick Astley - Never Gonna Give You Up")
    vc.current.author, vc.current.identifier = "Rick Astley - Topic", "dQw4w9WgXcQ"
    cog.estacion(1).letra.activa = True

    await _arranca(cog, vc)
    await _publicada(vc)
    await _arranca(cog, vc)         # el mismo tema otra vez (loop): ya no se busca
    await _publicada(vc, 2)

    assert en_vivo.busquedas == [("Rick Astley - Never Gonna Give You Up", "Rick Astley", "Never Gonna Give You Up", 180_000)]
    assert list(cog.letras._datos) == [music_mod.resolver.clave_de_pista(vc.current)]


async def test_si_habla_el_dj_la_letra_lo_dice_y_el_tema_que_sigue_la_vuelve_a_publicar(cog, en_vivo):
    vc = _sonando(en_vivo)
    cog.estacion(1).letra.activa = True
    await _arranca(cog, vc)
    await _publicada(vc)

    await cog.on_wavelink_track_start(FakePayloadInicio(vc, _clip_del_dj()))

    assert en_vivo.registro[-1] == ("edita", HABLA_EL_DJ) and cog.estacion(1).letra.tarea is None
    await _arranca(cog, vc, FakeTrack("Tema B"))
    await _publicada(vc, 2)
    texto_b = armar_texto("Tema B", LETRA_EN_VIVO.lineas, 0)
    assert en_vivo.registro[-3:] == [("borra", HABLA_EL_DJ), "panel", ("publica", texto_b)]
    assert [m.contenido for m in vc.text_channel.vivos] == [texto_b]


async def test_un_tema_sin_tiempos_se_publica_con_el_aviso_debajo_del_panel(cog, en_vivo):
    en_vivo.letra = Letra(False, [(None, "uno"), (None, "dos")])
    vc = _sonando(en_vivo)
    cog.estacion(1).letra.activa = True

    await _arranca(cog, vc)
    await _publicada(vc)

    assert en_vivo.registro == ["panel", ("publica", SIN_TIEMPOS)]


async def test_sin_canal_de_texto_el_tema_arranca_sin_letra(cog, en_vivo):
    vc = _sonando(en_vivo, con_canal=False)
    cog.estacion(1).letra.activa = True

    await _arranca(cog, vc)

    assert cog.estacion(1).letra.tarea is None and en_vivo.busquedas == []


async def test_al_acabarse_la_cola_la_letra_se_apaga_y_su_mensaje_se_borra(cog, en_vivo):
    vc = _sonando(en_vivo)
    cog.estacion(1).letra.activa = True
    await _arranca(cog, vc)
    await _publicada(vc)
    tarea = cog.estacion(1).letra.tarea

    await cog.on_wavelink_track_end(FakePayloadFin(vc, vc.current))

    est = cog.estacion(1)
    assert not est.letra.activa and est.letra.mensaje is None and vc.text_channel.vivos == []
    await asyncio.gather(tarea, return_exceptions=True)
    assert tarea.cancelled()


@pytest.mark.parametrize("motivo, queda_en_cola", [("finished", True), ("replaced", False)])
async def test_si_la_transmision_sigue_la_letra_sigue_prendida(cog, en_vivo, motivo, queda_en_cola):
    vc = _sonando(en_vivo)
    if queda_en_cola:
        vc.queue.append(FakeTrack("Tema B"))
    cog.estacion(1).letra.activa = True
    await _arranca(cog, vc)
    await _publicada(vc)

    await cog.on_wavelink_track_end(FakePayloadFin(vc, vc.current, reason=motivo))

    assert cog.estacion(1).letra.activa and len(vc.text_channel.vivos) == 1


def test_la_cache_de_letras_es_una_por_cog():
    assert Music(bot=None).letras is not Music(bot=None).letras


# --- el autoplay del panel -------------------------------------------------------------

def test_poner_autoplay_lo_aplica_al_player_y_devuelve_el_aviso(cog):
    vc = FakePlayer(auto=[FakeTrack("recomendada")])

    encendido = cog.poner_autoplay(1, vc, True)
    assert "encendido" in encendido and cog.autoplay_activo(1) and vc.autoplay is wavelink.AutoPlayMode.enabled
    assert len(vc.auto_queue) == 1      # encender no descarta lo ya recomendado

    apagado = cog.poner_autoplay(1, vc, False)
    assert "apagado" in apagado and not cog.autoplay_activo(1) and vc.autoplay is wavelink.AutoPlayMode.partial
    assert not vc.auto_queue


def test_poner_autoplay_sin_player_solo_guarda_la_preferencia(cog):
    assert "apagado" in cog.poner_autoplay(5, None, False)
    assert cog.autoplay_activo(5) is False


async def test_el_comando_autoplay_dice_lo_mismo_que_poner_autoplay(cog):
    ctx = FakeCtx(FakePlayer())

    await cog.autoplay_cmd(cog, ctx, "on")
    await cog.autoplay_cmd(cog, ctx, "off")

    assert ctx.enviados == [cog.poner_autoplay(1, None, True), cog.poner_autoplay(1, None, False)]


# --- el panel de botones y el cog ----------------------------------------------------------

def _interaccion_de_panel(vc):
    """Quien toca el panel, con el cog de verdad del otro lado."""
    yo = SimpleNamespace(guild=vc.guild, editados=[], privados=[], data={})

    async def edit_message(**kwargs):
        yo.editados.append(kwargs)

    async def send_message(contenido, **kwargs):
        yo.privados.append(contenido)

    yo.response = SimpleNamespace(edit_message=edit_message, send_message=send_message)
    return yo


def _player_de_panel():
    vc = FakePlayer(playing=True)
    vc.guild.voice_client = vc
    vc.current, vc.volume, vc.queue.mode = FakeTrack("Tema A"), 100, wavelink.QueueMode.normal
    return vc


async def test_el_boton_de_autoplay_usa_la_logica_del_cog_y_repinta_el_panel(cog):
    vc = _player_de_panel()
    vc.auto_queue.append(FakeTrack("recomendada"))
    cog._autoplay[1] = False
    vista = PlayerPanel(cog)

    yo = _interaccion_de_panel(vc)
    await vista.autoplay.callback(yo)
    assert cog.autoplay_activo(1) and vc.autoplay is wavelink.AutoPlayMode.enabled
    assert vista.autoplay.style is discord.ButtonStyle.success and len(yo.editados) == 1 and yo.privados == []

    yo = _interaccion_de_panel(vc)
    await vista.autoplay.callback(yo)
    assert not cog.autoplay_activo(1) and vc.autoplay is wavelink.AutoPlayMode.partial
    assert not vc.auto_queue and vista.autoplay.style is discord.ButtonStyle.secondary
    assert len(yo.editados) == 1 and yo.privados == []


async def test_el_boton_de_la_letra_del_panel_sigue_a_la_letra_del_cog(cog, en_vivo):
    vc = _sonando(en_vivo)
    vc.queue.mode, vc.volume = wavelink.QueueMode.normal, 100
    vista = PlayerPanel(cog)

    yo = _interaccion_de_panel(vc)
    await vista.letra_en_vivo.callback(yo)
    assert cog.estacion(1).letra.activa and vista.letra_en_vivo.style is discord.ButtonStyle.success
    assert len(yo.editados) == 1 and yo.privados == []

    yo = _interaccion_de_panel(vc)
    await vista.letra_en_vivo.callback(yo)
    assert not cog.estacion(1).letra.activa and vista.letra_en_vivo.style is discord.ButtonStyle.secondary


async def test_el_panel_sale_publicado_con_los_botones_ya_pintados(cog):
    vc = _player_de_panel()
    vc.paused, vc.queue.mode, vc.text_channel = True, wavelink.QueueMode.loop, FakeCanalTexto()
    cog._autoplay[1] = True
    publicados = []

    async def _enviar(contenido=None, **kwargs):
        publicados.append(kwargs)
        return SimpleNamespace()

    vc.text_channel.send = _enviar
    await cog._post_panel(vc.guild, vc)

    vista = publicados[0]["view"]
    assert isinstance(vista, PlayerPanel) and publicados[0]["embed"].title.startswith("⏸️")
    assert vista.toggle_pause.emoji.name == "▶️"
    assert vista.loop.style is vista.autoplay.style is discord.ButtonStyle.success
    assert vista.letra_en_vivo.style is discord.ButtonStyle.secondary
    assert cog.estacion(1).panel[1] is vista


class BotQueRegistraVistas:
    """Valida como `Client.add_view`: solo acepta vistas persistentes."""

    def __init__(self):
        self.vistas = []

    def add_view(self, vista):
        if not vista.is_persistent():
            raise ValueError("View is not persistent")
        self.vistas.append(vista)


async def test_al_cargar_el_cog_registra_la_vista_persistente_del_panel(cog, monkeypatch):
    monkeypatch.setattr(music_mod.DJ, "load_feed_urls", staticmethod(lambda: []))
    cog.bot = BotQueRegistraVistas()

    await cog.cog_load()

    assert cog.bot.vistas == [cog._vista] and isinstance(cog._vista, PlayerPanel)
    await cog.cog_unload()
    assert cog._vista.is_finished()


async def test_sin_bot_el_cog_carga_sin_registrar_vista(cog, monkeypatch):
    monkeypatch.setattr(music_mod.DJ, "load_feed_urls", staticmethod(lambda: []))

    await cog.cog_load()
    await cog.cog_unload()

    assert cog._vista is None


# --- al apagar el bot --------------------------------------------------------------------------

class MensajeDePanel:
    def __init__(self, falla=False):
        self.borrado, self.falla = False, falla

    async def delete(self):
        if self.falla:
            raise RuntimeError("se cayó")
        self.borrado = True


class VistaDePanel:
    def __init__(self):
        self.detenida = False

    def stop(self):
        self.detenida = True


CIERRES = [
    pytest.param(lambda cog: cog.guardar_y_cerrar(), id="guardar_y_cerrar"),
    pytest.param(lambda cog: cog.cog_unload(), id="cog_unload"),
]


async def _con_panel_y_letra(cog, en_vivo, guild_id):
    """Un servidor con el panel publicado y la letra en vivo ya en el chat."""
    vc = _sonando(en_vivo)
    vc.guild = FakeGuild(guild_id)
    vc.guild.voice_client = vc
    est = cog.estacion(guild_id)
    est.panel = (MensajeDePanel(), VistaDePanel())
    await cog.alternar_letra(vc.guild)
    await _publicada(vc)
    return vc, est


@pytest.mark.parametrize("cerrar", CIERRES)
async def test_al_apagar_se_borran_el_panel_y_la_letra_de_cada_servidor(cog, en_vivo, cerrar):
    servidores = [await _con_panel_y_letra(cog, en_vivo, guild_id) for guild_id in (1, 2)]
    paneles = [est.panel for _, est in servidores]
    tareas = [est.letra.tarea for _, est in servidores]

    await cerrar(cog)

    for (vc, est), (mensaje, vista), tarea in zip(servidores, paneles, tareas):
        assert mensaje.borrado and vista.detenida and est.panel is None
        assert vc.text_channel.vivos == [] and est.letra.mensaje is None
        await asyncio.gather(tarea, return_exceptions=True)
        assert tarea.cancelled()


@pytest.mark.parametrize("cerrar", CIERRES)
async def test_si_un_mensaje_no_se_deja_borrar_el_apagado_sigue_con_los_demas(cog, cerrar, caplog):
    roto, bueno = cog.estacion(1), cog.estacion(2)
    roto.panel = (MensajeDePanel(falla=True), VistaDePanel())
    bueno.panel = (MensajeDePanel(), VistaDePanel())

    with caplog.at_level("ERROR", logger="music"):
        await cerrar(cog)

    assert bueno.panel is None
    assert "No pude borrar los mensajes de 1" in caplog.text


async def test_guardar_y_cerrar_borra_los_mensajes_aunque_no_se_haya_retomado_nada(cog):
    est = cog.estacion(1)
    est.panel = (MensajeDePanel(), VistaDePanel())
    mensaje = est.panel[0]

    await cog.guardar_y_cerrar()

    assert cog._persistencia == "cerrada" and mensaje.borrado
