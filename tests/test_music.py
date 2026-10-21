"""El cog de música: lo que decide el bot antes de tocarle la puerta a Lavalink.

Los dobles imitan solo lo que el cog realmente usa del player de wavelink: la
cola (con `put_at`, que es lo que hace que `!playnext` funcione), la cola del
autoplay, y el par de banderas de estado.
"""

import asyncio

import pytest
import wavelink
from discord.ext import commands

from cogs import music as music_mod
from cogs.music import Music
from core import config
from core.dj import Clip


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


def test_el_proximo_mira_primero_la_cola_y_despues_el_autoplay(cog):
    pedido, sugerido = FakeTrack("pedida"), FakeTrack("sugerida")
    assert cog._proximo(FakePlayer(temas=[pedido], auto=[sugerido])) is pedido
    assert cog._proximo(FakePlayer(auto=[sugerido])) is sugerido
    assert cog._proximo(FakePlayer()) is None


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

    for _ in range(music_mod.REINTENTOS_POR_PISTA):
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
    assert cog._reintentos != {}

    await cog.on_wavelink_track_end(FakePayloadFin(vc, pista))

    assert cog._reintentos == {}


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

    for vuelta in range(music_mod.REINTENTOS_POR_PISTA):
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
    monkeypatch.setattr(
        music_mod.resolver, "motivo_definitivo", lambda pista: "está bloqueada"
    )
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

    for _ in range(music_mod.REINTENTOS_POR_PISTA + 1):
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

    for _ in range(music_mod.REINTENTOS_POR_PISTA + 1):
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
    monkeypatch.setattr(Music, "_clip_a_track", falso_track)
    vc = _reproductor_con_canal()
    vc.queue.append(FakeTrack("la que sigue"))

    for _ in range(music_mod.REINTENTOS_POR_PISTA + 1):
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

    for _ in range(music_mod.REINTENTOS_POR_PISTA + 1):
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

    for _ in range(music_mod.REINTENTOS_POR_PISTA + 1):
        assert not await _falla_como_en_lavalink(cog, vc, pista)

    # Lo que suena después (el aviso del DJ, el próximo tema) tiene que avanzar.
    assert vc._error_count < music_mod.TOPE_DE_WAVELINK


@pytest.mark.asyncio
async def test_varios_temas_perdidos_seguidos_paran_la_cola(cog, sin_espera):
    vc = _reproductor_con_canal()
    vc._error_count = 0
    trabadas = []

    for n in range(music_mod.CAIDAS_SEGUIDAS_MAX):
        pista = FakeTrack(f"caída {n}")
        for _ in range(music_mod.REINTENTOS_POR_PISTA + 1):
            trabadas.append(await _falla_como_en_lavalink(cog, vc, pista))

    assert trabadas[-1] and not any(trabadas[:-1])
    assert "paro la cola" in vc.text_channel.enviados[-1]


@pytest.mark.asyncio
async def test_una_cancion_entera_corta_la_racha_de_temas_perdidos(cog, sin_espera):
    vc = _reproductor_con_canal()
    vc._error_count = 0

    for n in range(music_mod.CAIDAS_SEGUIDAS_MAX - 1):
        pista = FakeTrack(f"caída {n}")
        for _ in range(music_mod.REINTENTOS_POR_PISTA + 1):
            await _falla_como_en_lavalink(cog, vc, pista)
    await cog.on_wavelink_track_end(FakePayloadFin(vc, FakeTrack("una que sí sonó")))

    pista = FakeTrack("otra caída")
    for _ in range(music_mod.REINTENTOS_POR_PISTA + 1):
        assert not await _falla_como_en_lavalink(cog, vc, pista)


@pytest.mark.asyncio
async def test_play_retoma_una_cola_parada_por_fallos(cog):
    tema = FakeTrack("la que espera")
    vc = FakePlayer(temas=[tema])
    vc._error_count = music_mod.TOPE_DE_WAVELINK
    cog._caidas[vc.guild.id] = music_mod.CAIDAS_SEGUIDAS_MAX

    await cog._arrancar_si_hace_falta(vc)

    assert vc.tocando == [tema]
    assert vc._error_count == 0
    assert vc.guild.id not in cog._caidas


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
