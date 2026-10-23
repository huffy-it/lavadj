"""El cog de música: lo que decide el bot antes de tocarle la puerta a Lavalink."""

import asyncio
import contextlib
import re
import time
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
import wavelink
from discord.ext import commands

from cogs import music as music_mod
from cogs.music import Music
from core import api, config, persistencia  # noqa: F401 - api: lo importaba la suite original
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
# --- dobles y ayudas -------------------------------------------------------------

class FakeTrack:
    def __init__(self, title="Un tema"):
        self.title, self.length, self.identifier = title, 180_000, title

class FakeQueue(list):
    mode = wavelink.QueueMode.normal
    async def put_wait(self, track):
        self.append(track)
        return 1
    def put_at(self, index, track):
        self.insert(index, track)
    def get(self):
        return self.pop(0)

class FakeGuild:
    def __init__(self, guild_id=1):
        self.id, self.voice_client, self.canales = guild_id, None, {}
    def get_channel(self, id_):
        return self.canales.get(id_)

    get_channel_or_thread = get_channel

class FakeCanalTexto:
    def __init__(self):
        self.enviados = []
    async def send(self, contenido=None, **kwargs):
        self.enviados.append(contenido)

class FakePlayer:
    def __init__(self, playing=False, temas=(), auto=(), guild=None):
        self.playing, self.paused, self.connected = playing, False, True
        self.queue, self.auto_queue = FakeQueue(temas), FakeQueue(auto)
        self.guild = guild or FakeGuild()
        self.channel = "canal-de-voz"
        self.autoplay = wavelink.AutoPlayMode.disabled
        self.tocando, self.llamadas, self.falla_el_play = [], [], False
        self.volume, self.current = 100, None
    async def play(self, track, **kwargs):
        if self.falla_el_play:
            raise RuntimeError("Lavalink no responde")
        self.tocando.append(track)
        self.llamadas.append((track, kwargs))
        self.playing = True
    async def disconnect(self):
        self.connected = False

class FakeAuthor:
    def __init__(self, channel="canal-de-voz", nombre="ana"):
        self.voice = SimpleNamespace(channel=channel) if channel else None
        self.display_name = nombre

class FakeCtx:
    def __init__(self, vc=None, author=None, guild=None, interaction=None):
        self.voice_client, self.author, self.interaction = vc, author or FakeAuthor(), interaction
        self.guild = guild or (vc.guild if vc else FakeGuild())
        self.enviados, self.opciones, self.eventos = [], [], []
    def typing(self):
        return contextlib.nullcontext()
    async def defer(self, *, ephemeral=False):
        self.eventos.append("defer")
    async def send(self, contenido=None, **kwargs):
        self.enviados.append(contenido)
        self.opciones.append(kwargs)
        return contenido
    @property
    def ultimo(self):
        return self.enviados[-1] if self.enviados else ""

class Payload:
    def __init__(self, player, track, reason="finished", exception="400"):
        self.player, self.track, self.original, self.reason, self.exception = player, track, track, reason, exception

class MensajeDePanel:
    def __init__(self, falla=False, canal=None):
        self.borrado, self.falla, self.channel = False, falla, canal
    async def delete(self):
        if self.falla:
            raise RuntimeError("se cayó")
        self.borrado = True

class VistaDePanel:
    detenida = False
    def stop(self):
        self.detenida = True

class FakeCanalDeVoz:
    def __init__(self, id_=10, guild=None, humanos=1):
        self.id, self.guild = id_, guild or FakeGuild()
        self.members = [SimpleNamespace(bot=False)] * humanos + [SimpleNamespace(bot=True)]
        self.conexiones, self.clase, self.player = 0, None, None
        self.falla_al_conectar = self.falla_al_tocar = False
    async def connect(self, *, cls):
        self.conexiones += 1
        if self.falla_al_conectar:
            raise RuntimeError("timeout")
        self.clase = cls
        self.player = PlayerRetomable(self.guild)
        self.player.falla_el_play = self.falla_al_tocar
        self.guild.voice_client = self.player
        return self.player

class PlayerRetomable(FakePlayer):
    def __init__(self, guild):
        super().__init__(guild=guild)
    async def play(self, track, **kwargs):
        await super().play(track, **kwargs)
        self.current = track
    async def set_volume(self, valor):
        self.volume = valor
    async def disconnect(self):
        await super().disconnect()
        self.guild.voice_client = None

class PlayerDeSesionVieja(FakePlayer):
    def __init__(self, actual=None, posicion=0, **kwargs):
        super().__init__(playing=actual is not None, **kwargs)
        self.current, self._last_position = actual, posicion
        self._last_update, self._inicio_ns = 2, 1
        self.voces, self.rechaza_la_voz = 0, False
        self.guild.voice_client = self
    async def _dispatch_voice_update(self):
        self.voces += 1
        if self.rechaza_la_voz:
            self.connected = False

class PlayerQueResuelve(FakePlayer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.resolviendo, self.suelta = 0, asyncio.Event()
    async def play(self, track, **kwargs):
        self.resolviendo += 1
        try:
            await self.suelta.wait()
            await super().play(track, **kwargs)
        finally:
            self.resolviendo -= 1

class PlayerQueRevientaAlMirarlo(FakePlayer):
    @property
    def paused(self):
        raise RuntimeError("el player se estaba desconectando")
    @paused.setter
    def paused(self, valor):
        pass

class ResolverQueAnota:
    def __init__(self):
        self.precargadas = []
    def precargar(self, pista):
        self.precargadas.append(pista)

@pytest.fixture
def cog():
    cog = Music(bot=None)
    cog.dj.enabled = False
    return cog

@pytest.fixture
def sin_espera(monkeypatch):
    async def _dormir(_segundos):
        return None
    monkeypatch.setattr(music_mod.asyncio, "sleep", _dormir)

@pytest.fixture
def ticks(monkeypatch):
    """`asyncio.sleep` que solo cede el turno; devuelve el `sleep` verdadero."""
    esperar = asyncio.sleep
    async def _ceder(_segundos):
        await esperar(0)

    monkeypatch.setattr(music_mod.asyncio, "sleep", _ceder)
    return esperar

@pytest.fixture
def sin_panel(monkeypatch):
    """Ni panel ni desconexión por inactividad; devuelve los players donde se habría publicado el panel."""
    publicados = []
    async def _panel(self, guild, vc):
        publicados.append(vc)
    async def _sin_desconexion(self, guild, vc):
        return None

    monkeypatch.setattr(Music, "_post_panel", _panel)
    monkeypatch.setattr(Music, "_schedule_idle_disconnect", _sin_desconexion)
    return publicados

@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.setattr(music_mod.DJ, "load_feed_urls", staticmethod(lambda: []))

def _busca(monkeypatch, resultado=None, sin=()):
    """Cambia `Playable.search` y devuelve la lista donde anota cada consulta; sin `resultado` encuentra todo menos `sin`."""
    consultas = []
    async def _search(query, *a, **kw):
        consultas.append(query)
        if isinstance(resultado, Exception):
            raise resultado
        if resultado is None:
            return [] if query in sin else [FakeTrack(query)]
        return resultado

    monkeypatch.setattr(music_mod.wavelink.Playable, "search", _search)
    return consultas

def _conectar(monkeypatch, vc, **kwargs):
    async def _connect(self, ctx):
        return vc
    monkeypatch.setattr(Music, "_connect_voice", _connect)
    return FakeCtx(vc, **kwargs)

def _con_canal(**kwargs):
    vc = FakePlayer(playing=True, **kwargs)
    vc.text_channel = FakeCanalTexto()
    return vc

def _falla(cog, vc, pista):
    return cog.on_wavelink_track_exception(Payload(vc, pista))

def _fin(cog, vc, pista, reason="finished"):
    return cog.on_wavelink_track_end(Payload(vc, pista, reason))

def _inicio(cog, vc, pista):
    return cog.on_wavelink_track_start(Payload(vc, pista))

def _clip_del_dj(uri=None):
    clip = FakeTrack("corte del DJ")
    clip.is_dj_clip = True
    if uri:
        clip.uri = uri
    return clip

def _pista_resuelta(titulo, identifier, id_youtube="abc"):
    pista = FakeTrack(titulo)
    pista.identifier = identifier
    pista.extras = {"yt_origen": {"identifier": id_youtube, "title": titulo}}
    return pista

def _tema(id_="abc", largo=200_000):
    tema = FakeTrack(id_)
    tema.length, tema.source = largo, "youtube"
    tema.uri = f"https://www.youtube.com/watch?v={id_}"
    return tema

@pytest.fixture
def wavs(tmp_path, monkeypatch):
    monkeypatch.setattr(music_mod.tts, "CACHE_DIR", tmp_path)
    pistas = []
    for ruta in (tmp_path / "dj_abc.wav", tmp_path.parent / "cancion.wav"):
        ruta.write_bytes(b"RIFF")
        pista = FakeTrack(ruta.stem)
        pista.uri = str(ruta)
        pistas.append(pista)
    return pistas

async def test_playnext_mete_el_tema_al_frente(cog, monkeypatch):
    vc = FakePlayer(playing=True, temas=[FakeTrack("vieja 1"), FakeTrack("vieja 2")])
    ctx = _conectar(monkeypatch, vc)
    _busca(monkeypatch, [FakeTrack("la nueva")])
    await cog.playnext(cog, ctx, query="la nueva")
    assert [t.title for t in vc.queue] == ["la nueva", "vieja 1", "vieja 2"]
    assert "La próxima" in ctx.ultimo

async def test_playnext_no_se_come_una_playlist_entera(cog, monkeypatch):
    vc = FakePlayer(playing=True)
    ctx = _conectar(monkeypatch, vc)
    playlist = wavelink.Playlist.__new__(wavelink.Playlist)
    playlist.name, playlist.tracks = "Una lista larga", [FakeTrack(str(i)) for i in range(100)]
    _busca(monkeypatch, playlist)
    await cog.playnext(cog, ctx, query="https://youtube.com/playlist?list=x")
    assert not vc.queue and "`!play`" in ctx.ultimo

async def test_playnext_respeta_el_tope_de_la_cola(cog, monkeypatch):
    vc = FakePlayer(playing=True, temas=[FakeTrack(str(i)) for i in range(config.QUEUE_LIMIT)])
    ctx = _conectar(monkeypatch, vc)
    await cog.playnext(cog, ctx, query="una más")
    assert len(vc.queue) == config.QUEUE_LIMIT and "llena" in ctx.ultimo

async def test_el_comando_autoplay_pasa_por_poner_autoplay_y_al_apagar_tira_lo_recomendado(cog):
    """Si no se tira lo ya recomendado, sigue sonando un rato y parece que el comando no hizo nada."""
    vc = FakePlayer(auto=[FakeTrack("recomendada")])
    ctx = FakeCtx(vc)
    await cog.autoplay_cmd(cog, ctx, "on")
    assert vc.autoplay is wavelink.AutoPlayMode.enabled and cog.autoplay_activo(1) and len(vc.auto_queue) == 1
    await cog.autoplay_cmd(cog, ctx, "off")
    assert vc.autoplay is wavelink.AutoPlayMode.partial and not vc.auto_queue
    assert ctx.enviados == [cog.poner_autoplay(1, None, True), cog.poner_autoplay(1, None, False)]

async def test_autoplay_sin_argumento_informa_el_estado(cog):
    ctx = FakeCtx(FakePlayer())
    await cog.autoplay_cmd(cog, ctx, "")
    assert "autoplay" in ctx.ultimo.lower()

def test_el_autoplay_es_por_servidor(cog):
    cog._autoplay[7] = False
    assert cog.autoplay_activo(7) is False
    assert cog.autoplay_activo(8) is config.AUTOPLAY_DEFAULT

def test_poner_autoplay_lo_aplica_al_player_y_devuelve_el_aviso(cog):
    vc = FakePlayer(auto=[FakeTrack("recomendada")])
    encendido = cog.poner_autoplay(1, vc, True)
    assert "encendido" in encendido and cog.autoplay_activo(1) and vc.autoplay is wavelink.AutoPlayMode.enabled
    assert len(vc.auto_queue) == 1
    apagado = cog.poner_autoplay(1, vc, False)
    assert "apagado" in apagado and not cog.autoplay_activo(1) and vc.autoplay is wavelink.AutoPlayMode.partial
    assert not vc.auto_queue

def test_poner_autoplay_sin_player_solo_guarda_la_preferencia(cog):
    assert "apagado" in cog.poner_autoplay(5, None, False)
    assert cog.autoplay_activo(5) is False

@pytest.mark.parametrize("vc, canal, falla", [
    (FakePlayer(), "otro-canal", True), (FakePlayer(), "canal-de-voz", False), (None, None, False),
], ids=["de-afuera", "de-adentro", "sin-bot-conectado"])
async def test_los_controles_son_de_quien_esta_escuchando(vc, canal, falla):
    predicado = music_mod.en_el_mismo_canal().predicate
    ctx = FakeCtx(vc, author=FakeAuthor(channel=canal))
    if falla:
        with pytest.raises(commands.CheckFailure):
            await predicado(ctx)
    else:
        assert await predicado(ctx)

@pytest.mark.parametrize("args, volumen, aviso", [
    (("vol", "40"), 40, "40"), (("volumen", "50"), 50, "50"),
    (("vol",), 120, "120"), (("vol", "fuerte"), 120, "0 y 200"),
], ids=["mueve", "alias-volumen", "sin-numero", "basura"])
async def test_dj_vol(cog, args, volumen, aviso):
    cog.dj.volume = 120
    ctx = FakeCtx()
    await cog.dj_cmd(cog, ctx, *args)
    assert cog.dj.volume == volumen and aviso in ctx.ultimo
# --- reintentos cuando Lavalink no puede sacar el audio ---------------------------------

async def test_una_pista_que_falla_vuelve_al_frente_de_la_cola(cog, sin_espera):
    vc = _con_canal()
    vc.queue.append(FakeTrack("la que sigue"))
    await _falla(cog, vc, FakeTrack("la que falló"))
    assert [t.title for t in vc.queue] == ["la que falló", "la que sigue"]
    assert vc.text_channel.enviados == []

@pytest.mark.parametrize("resuelta", [False, True], ids=["misma-pista", "otra-url-cada-vuelta"])
async def test_despues_de_agotar_los_reintentos_la_saltea_y_avisa(cog, sin_espera, resuelta):
    """Regresión: contar por el identifier (cambia con cada URL) reintentaba para siempre."""
    def pista(n):
        return _pista_resuelta("la maldita", f"rr{n}-url") if resuelta else FakeTrack("la maldita")

    vc = _con_canal()
    for n in range(REINTENTOS_POR_PISTA):
        await _falla(cog, vc, pista(n))
    vc.queue.clear()
    await _falla(cog, vc, pista(9))
    assert list(vc.queue) == [] and "la maldita" in vc.text_channel.enviados[-1]

async def test_el_clip_del_dj_no_se_reintenta(cog, sin_espera):
    vc = _con_canal()
    await _falla(cog, vc, _clip_del_dj(f"{music_mod.tts.CACHE_DIR}/dj_abc.wav"))
    assert list(vc.queue) == []

async def test_si_la_pista_termina_bien_recupera_todos_sus_intentos(cog, sin_espera, sin_panel):
    vc = _con_canal()
    pista = FakeTrack("la intermitente")
    await _falla(cog, vc, pista)
    await _inicio(cog, vc, pista)
    assert cog.estacion(vc.guild.id).reintentos != {}   # arrancar no basta: un tema que revienta al decodificar reiniciaría la cuenta
    await _fin(cog, vc, pista)
    assert cog.estacion(vc.guild.id).reintentos == {}

async def test_un_video_sin_caso_se_saltea_sin_gastar_reintentos(cog, sin_espera, monkeypatch):
    monkeypatch.setattr(cog.resolver, "motivo_definitivo", lambda pista: "está bloqueada")
    vc = _con_canal()
    await _falla(cog, vc, FakeTrack("la bloqueada"))
    assert list(vc.queue) == []
    assert "está bloqueada" in vc.text_channel.enviados[-1] and "la bloqueada" in vc.text_channel.enviados[-1]

async def test_la_cancion_se_nombra_por_su_titulo_de_youtube(cog, sin_espera):
    """Regresión: los avisos salían como "Unknown title"; el nombre bueno está en el origen guardado."""
    vc = _con_canal()
    pista = FakeTrack("Unknown title")
    pista.identifier = "rr4-url"
    pista.extras = {"yt_origen": {"identifier": "abc", "title": "Gato por liebre"}}
    for _ in range(REINTENTOS_POR_PISTA + 1):
        await _falla(cog, vc, pista)
    assert "Gato por liebre" in vc.text_channel.enviados[-1]
    assert "Unknown title" not in vc.text_channel.enviados[-1]

async def test_el_cog_le_cuenta_a_la_salud_como_viene_la_cosa(cog, sin_espera, sin_panel):
    vc = _con_canal()
    await _falla(cog, vc, FakeTrack("una"))
    await _falla(cog, vc, FakeTrack("otra"))
    assert cog.salud.estado(al_aire=True, hay_cola=True)["estado"] == "flojo"
    await _inicio(cog, vc, FakeTrack("la que zafó"))
    assert cog.salud.estado(al_aire=True, hay_cola=True)["estado"] == "bien"

async def test_cuando_se_rinde_con_un_tema_el_dj_lo_dice(cog, sin_espera, monkeypatch):
    dicho = []
    async def falso_aviso(texto):
        dicho.append(texto)

    monkeypatch.setattr(cog.dj, "aviso", falso_aviso)
    vc = _con_canal()
    for _ in range(REINTENTOS_POR_PISTA + 1):
        await _falla(cog, vc, FakeTrack("la maldita"))
    assert dicho and "la maldita" in dicho[0]

async def test_el_aviso_hablado_va_al_frente_de_la_cola(cog, sin_espera, monkeypatch):
    locucion = FakeTrack("clip del aviso")
    async def falso_aviso(texto):
        return Clip(path="/tmp/aviso.wav", text=texto, for_source=None, kind="aviso")
    async def falso_track(self, clip):
        return locucion

    monkeypatch.setattr(cog.dj, "aviso", falso_aviso)
    monkeypatch.setattr(Locutor, "_clip_a_track", falso_track)
    vc = _con_canal()
    vc.queue.append(FakeTrack("la que sigue"))
    for _ in range(REINTENTOS_POR_PISTA + 1):
        await _falla(cog, vc, FakeTrack("la maldita"))
    assert vc.queue[0] is locucion

async def test_no_dice_nada_si_no_esta_en_un_canal(cog, sin_espera, monkeypatch):
    dicho = []
    async def falso_aviso(texto):
        dicho.append(texto)

    monkeypatch.setattr(cog.dj, "aviso", falso_aviso)
    vc = _con_canal()
    vc.connected = False
    for _ in range(REINTENTOS_POR_PISTA + 1):
        await _falla(cog, vc, FakeTrack("la maldita"))
    assert dicho == []

async def test_la_pista_vuelve_a_la_cola_antes_de_ponerse_a_esperar(cog, monkeypatch):
    """Regresión: wavelink mira la cola apenas muere el tema; reencolar tras un await dejaba el player estacionado."""
    vc = _con_canal()
    cola_al_dormir = []
    async def espera_espia(_segundos):
        cola_al_dormir.append(list(vc.queue))

    monkeypatch.setattr(music_mod.asyncio, "sleep", espera_espia)
    await _falla(cog, vc, FakeTrack("la que falló"))
    assert list(vc.queue), "no la reencoló"
    assert all(cola_al_dormir), "se puso a esperar con la cola todavía vacía"

async def test_al_terminar_solo_se_borra_el_wav_del_dj(cog, wavs):
    for pista in wavs:
        await _fin(cog, _con_canal(), pista)
    assert not Path(wavs[0].uri).exists() and Path(wavs[1].uri).exists()

async def test_un_tema_que_falla_antes_del_primer_reporte_igual_se_reintenta(cog, sin_espera):
    """Regresión: wavelink deja `_last_update` en None hasta el primer playerUpdate y la cuenta de reanudar reventaba."""
    vc = _con_canal()
    vc._last_update, vc._inicio_ns = None, 123
    caida = FakeTrack("la primera de la noche")
    await _falla(cog, vc, caida)
    assert list(vc.queue) == [caida]

async def _agotar(cog, vc, pista):
    """Falla `pista` hasta rendirse como wavelink (suma a `_error_count` tras el handler); dice si la cola quedó trabada en cada fallo."""
    vc._error_count, trabadas = 0, []
    for _ in range(REINTENTOS_POR_PISTA + 1):
        await _falla(cog, vc, pista)
        trabadas.append(vc._error_count >= music_mod.TOPE_DE_WAVELINK)
        vc._error_count += 1
    return trabadas

async def test_un_tema_que_agota_sus_reintentos_no_traba_la_cola(cog, sin_espera):
    vc = _con_canal()
    assert not any(await _agotar(cog, vc, FakeTrack("la maldita")))
    assert vc._error_count < music_mod.TOPE_DE_WAVELINK

async def test_varios_temas_perdidos_seguidos_paran_la_cola(cog, sin_espera):
    vc = _con_canal()
    trabadas = []
    for n in range(CAIDAS_SEGUIDAS_MAX):
        trabadas += await _agotar(cog, vc, FakeTrack(f"caída {n}"))
    assert trabadas[-1] and not any(trabadas[:-1])
    assert "paro la cola" in vc.text_channel.enviados[-1]

async def test_una_cancion_entera_corta_la_racha_de_temas_perdidos(cog, sin_espera):
    vc = _con_canal()
    for n in range(CAIDAS_SEGUIDAS_MAX - 1):
        await _agotar(cog, vc, FakeTrack(f"caída {n}"))
    await _fin(cog, vc, FakeTrack("una que sí sonó"))
    assert not any(await _agotar(cog, vc, FakeTrack("otra caída")))

async def test_dos_arranques_a_la_vez_no_se_comen_un_tema(cog):
    a, b = FakeTrack("a"), FakeTrack("b")
    vc = PlayerQueResuelve(temas=[a, b])
    tareas = [asyncio.create_task(cog._arrancar_si_hace_falta(vc)) for _ in range(2)]
    await asyncio.sleep(0)
    vc.suelta.set()
    await asyncio.gather(*tareas)
    assert vc.tocando == [a] and list(vc.queue) == [b]

async def test_no_arranca_encima_del_autoplay_que_esta_resolviendo(cog):
    b = FakeTrack("b")
    vc = PlayerQueResuelve(temas=[b])
    vc.resolviendo = 1     # el autoplay ya sacó su tema y espera a yt-dlp
    vc.suelta.set()
    await cog._arrancar_si_hace_falta(vc)
    assert vc.tocando == [] and list(vc.queue) == [b]
# --- reinicio del nodo de audio ----------------------------------------------------------

async def test_un_nodo_reiniciado_retoma_el_tema_donde_iba(cog):
    sonando, siguiente = FakeTrack("sonando"), FakeTrack("siguiente")
    vc = PlayerDeSesionVieja(actual=sonando, posicion=95_000, temas=[siguiente])
    vc.paused = True
    await cog.recuperar_players([vc])
    assert vc.voces == 1 and vc.connected and list(vc.queue) == [siguiente]
    assert vc.llamadas == [(sonando, {"start": 95_000, "paused": True, "add_history": False})]

async def test_un_nodo_reiniciado_no_reanuda_desde_la_posicion_del_tema_anterior(cog):
    vc = PlayerDeSesionVieja(actual=FakeTrack("nuevo"), posicion=95_000)
    vc._last_update, vc._inicio_ns = 1, 2
    await cog.recuperar_players([vc])
    assert vc.llamadas[0][1]["start"] == 0

async def test_un_nodo_reiniciado_con_el_player_ocioso_solo_rehace_la_voz(cog):
    vc = PlayerDeSesionVieja()
    await cog.recuperar_players([vc])
    assert vc.voces == 1 and vc.llamadas == [] and vc.connected

async def test_si_lavalink_rechaza_la_voz_se_corta_y_se_avisa(cog):
    vc = PlayerDeSesionVieja(actual=FakeTrack("sonando"), temas=[FakeTrack("otro")])
    vc.rechaza_la_voz, vc.text_channel = True, FakeCanalTexto()
    await cog.recuperar_players([vc])
    assert vc.llamadas == [] and not vc.connected and list(vc.queue) == []
    assert vc.text_channel.enviados == [music_mod.AVISO_NODO_REINICIADO]

async def test_si_no_se_puede_repetir_el_tema_se_corta_y_avisa_en_el_panel(cog):
    vc = PlayerDeSesionVieja(actual=FakeTrack("sonando"))
    vc.falla_el_play, vc.text_channel = True, FakeCanalTexto()
    canal_del_panel = FakeCanalTexto()
    cog.estacion(vc.guild.id).panel = (MensajeDePanel(canal=canal_del_panel), VistaDePanel())
    await cog.recuperar_players([vc])
    assert not vc.connected
    assert canal_del_panel.enviados == [music_mod.AVISO_NODO_REINICIADO] and vc.text_channel.enviados == []

async def test_un_player_roto_no_frena_a_los_demas(cog):
    roto = PlayerDeSesionVieja(actual=FakeTrack("roto"), guild=FakeGuild(1))
    roto.falla_el_play = True
    bueno = PlayerDeSesionVieja(actual=FakeTrack("bueno"), guild=FakeGuild(2))
    await cog.recuperar_players([roto, bueno])
    assert len(bueno.llamadas) == 1

async def test_al_cortar_se_borran_los_cortes_del_dj_que_esperaban_en_la_cola(cog, wavs):
    vc = PlayerDeSesionVieja(temas=wavs)
    await cog.teardown(vc.guild)
    assert not Path(wavs[0].uri).exists() and Path(wavs[1].uri).exists()
# --- el hueco entre pistas ---------------------------------------------------------------

@pytest.fixture
def reloj(cog):
    marca = {"t": 0.0}
    cog.salud = Salud(reloj=lambda: marca["t"])
    def avanzar(segundos):
        marca["t"] += segundos

    return avanzar

def _hueco_medido(cog):
    return cog.salud.estado(al_aire=True, hay_cola=True)["hueco"]

@pytest.mark.parametrize("razon, clip, segundos, en_log", [
    ("finished", False, 3.0, "la que sigue"), ("finished", True, 0.4, "corte del DJ"), ("replaced", False, 5.0, None),
], ids=["cancion", "corte-del-dj", "skip"])
async def test_el_hueco_entre_pistas_queda_registrado(cog, reloj, sin_panel, caplog, razon, clip, segundos, en_log):
    vc = _con_canal()
    vc.queue.append(FakeTrack("la que sigue"))
    reloj(100)
    with caplog.at_level("INFO", logger="music"):
        await _fin(cog, vc, FakeTrack("la que terminó"), razon)
        reloj(segundos)
        await _inicio(cog, vc, _clip_del_dj() if clip else FakeTrack("la que sigue"))
    assert _hueco_medido(cog)["ultimo"] == segundos
    assert f"{segundos} s" in caplog.text and (en_log is None or en_log in caplog.text)
    if clip:    # el hueco del clip no cuenta como que sonó música
        assert cog.salud.sin_sonar == pytest.approx(100 + segundos)

async def test_si_la_cola_queda_vacia_lo_que_arranque_despues_no_es_un_hueco(cog, reloj, sin_panel):
    vc = _con_canal()
    await _fin(cog, vc, FakeTrack("la última"))
    reloj(10.0)
    await _inicio(cog, vc, FakeTrack("un !play nuevo"))
    assert _hueco_medido(cog) is None

async def test_al_desconectar_el_final_pendiente_se_descarta(cog, reloj):
    vc = PlayerDeSesionVieja()
    cog.salud.termino(vc.guild.id)
    await cog.teardown(vc.guild)
    reloj(10.0)
    assert cog.salud.arranco(vc.guild.id) is None
# --- el resolver compartido --------------------------------------------------------------

def test_el_cog_toma_el_resolver_del_bot_o_arma_el_suyo():
    compartido = Resolver()
    assert Music(bot=SimpleNamespace(resolver=compartido)).resolver is compartido
    uno, otro = Music(bot=None), Music(bot=None)
    assert isinstance(uno.resolver, Resolver) and uno.resolver is not otro.resolver

async def test_al_conectar_el_player_comparte_el_resolver_del_cog(cog):
    canal = FakeCanalDeVoz()
    ctx = FakeCtx(author=FakeAuthor(channel=canal))
    ctx.channel = "canal-de-texto"
    vc = await cog._connect_voice(ctx)
    assert vc is canal.player and canal.clase is music_mod.resolver.ResolvingPlayer
    assert vc.resolver is cog.resolver

async def test_el_cog_saltea_el_video_que_marco_el_resolver_compartido(sin_espera, monkeypatch):
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
    vc = _con_canal()
    await _falla(cog, vc, pista)
    assert list(vc.queue) == [] and "la borraron" in vc.text_channel.enviados[-1]
# --- precargar la URL del tema que viene -----------------------------------------------------

def _cancion_corta(titulo="la que suena"):
    """Un tema al que ya le quedan menos de `PRECARGAR_ANTES` s: su precarga no espera."""
    pista = FakeTrack(titulo)
    pista.length = 20_000
    return pista

async def _arranca_y_precarga(cog, vc, pista):
    vc.current = pista
    await _inicio(cog, vc, pista)
    if tarea := cog.estacion(vc.guild.id).precarga:
        await asyncio.wait_for(tarea, timeout=5)

async def test_al_arrancar_un_corte_del_dj_se_precarga_la_que_viene_despues(cog, sin_panel):
    vc = _con_canal()
    vc.resolver = ResolverQueAnota()
    despues = FakeTrack("la que viene después del corte")
    vc.queue.append(despues)
    await _inicio(cog, vc, _clip_del_dj())
    assert vc.resolver.precargadas == [despues] and cog.estacion(1).precarga is None

@pytest.mark.parametrize("con_clip", [False, True], ids=["cola-vacia", "sigue-un-corte"])
async def test_si_no_viene_nada_o_viene_un_corte_no_se_precarga(cog, sin_panel, con_clip):
    vc = _con_canal()
    vc.resolver = ResolverQueAnota()
    if con_clip:
        vc.queue.append(_clip_del_dj())
    await _arranca_y_precarga(cog, vc, _cancion_corta())
    await _inicio(cog, vc, _clip_del_dj())
    assert vc.resolver.precargadas == []

async def test_sin_resolver_en_el_player_se_usa_el_del_cog(cog, sin_panel):
    vc = _con_canal()
    assert not hasattr(vc, "resolver")
    cog.resolver = ResolverQueAnota()
    siguiente = FakeTrack("la que sigue")
    vc.queue.append(siguiente)
    await _arranca_y_precarga(cog, vc, _cancion_corta())
    assert cog.resolver.precargadas == [siguiente]

async def test_si_la_precarga_falla_el_panel_sale_igual(cog, sin_panel, caplog):
    class ResolverQueRevienta:
        def precargar(self, pista):
            raise RuntimeError("sin loop")

    vc = _con_canal()
    vc.resolver = ResolverQueRevienta()
    vc.queue.append(FakeTrack("la que sigue"))
    with caplog.at_level("WARNING", logger="music"):
        await _arranca_y_precarga(cog, vc, _cancion_corta())
    assert sin_panel == [vc] and "precargar" in caplog.text
# --- el vigilante que destraba la cola -----------------------------------------------------

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
    assert vc.tocando == [primera] and list(vc.queue) == [segunda]
    assert vc._error_count == 0 and cog.estacion(vc.guild.id).caidas == 0
    assert "Nada suena" in caplog.text and "2 en cola" in caplog.text

async def test_con_la_cola_vacia_destraba_con_el_tema_del_autoplay(cog):
    sugerida, otra = FakeTrack("sugerida"), FakeTrack("otra")
    vc = FakePlayer(auto=[sugerida, otra])
    vc._error_count = music_mod.TOPE_DE_WAVELINK
    cog.estacion(vc.guild.id).caidas = CAIDAS_SEGUIDAS_MAX
    await _revisar(cog, [vc], 2)
    assert vc.tocando == [sugerida] and list(vc.auto_queue) == [otra]
    assert vc._error_count == 0 and cog.estacion(vc.guild.id).caidas == 0

ESTADOS_NO_ATASCADOS = {
    "pausado": lambda vc: setattr(vc, "paused", True),
    "sonando": lambda vc: setattr(vc, "playing", True),
    "resolviendo": lambda vc: setattr(vc, "resolviendo", 1),
    "desconectado": lambda vc: setattr(vc, "connected", False),
    "sin-nada-en-cola": lambda vc: (vc.queue.clear(), vc.auto_queue.clear()),
}

@pytest.mark.parametrize("estado", ESTADOS_NO_ATASCADOS)
async def test_el_vigilante_no_toca_lo_que_no_esta_atascado(cog, caplog, estado):
    vc = FakePlayer(temas=[FakeTrack("de la cola")], auto=[FakeTrack("del autoplay")])
    ESTADOS_NO_ATASCADOS[estado](vc)
    with caplog.at_level("WARNING", logger="music"):
        await _revisar(cog, [vc], 4)
    assert vc.tocando == [] and "Nada suena" not in caplog.text

async def test_con_el_lock_del_servidor_tomado_no_es_un_atasco(cog):
    vc = FakePlayer(temas=[FakeTrack("de la cola")])
    lock = cog.estacion(vc.guild.id).arranque
    await lock.acquire()
    await _revisar(cog, [vc], 4)
    assert vc.tocando == []
    lock.release()
    await cog._vigilar_una_vez([vc])     # con el lock tomado no se anotó nada: otra vez hacen falta dos
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
    assert vc.tocando == [primero] and list(vc.queue) == [segundo]

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
    vc = FakePlayer(temas=[a, b])
    await _revisar(cog, [vc], 2)
    assert vc.tocando == [a]
    vc.playing = False      # el player sigue mudo
    await cog._vigilar_una_vez([vc])
    assert vc.tocando == [a]
    await cog._vigilar_una_vez([vc])
    assert vc.tocando == [a, b]

async def test_cada_servidor_lleva_su_propia_cuenta(cog):
    uno = FakePlayer(temas=[FakeTrack("uno")], guild=FakeGuild(1))
    otro = FakePlayer(temas=[FakeTrack("otro")], guild=FakeGuild(2))
    await cog._vigilar_una_vez([uno])
    await cog._vigilar_una_vez([uno, otro])
    assert len(uno.tocando) == 1 and otro.tocando == []

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

@pytest.mark.parametrize("roto_con", ["falla_el_play", "paused"])
async def test_un_player_que_falla_no_frena_la_revision_de_los_demas(cog, caplog, roto_con):
    clase = PlayerQueRevientaAlMirarlo if roto_con == "paused" else FakePlayer
    roto = clase(temas=[FakeTrack("del roto")], guild=FakeGuild(1))
    roto.falla_el_play = True
    bueno = FakePlayer(temas=[FakeTrack("del bueno")], guild=FakeGuild(2))
    with caplog.at_level("ERROR", logger="music"):
        await _revisar(cog, [roto, bueno], 2)
    assert len(bueno.tocando) == 1 and "No pude revisar" in caplog.text

async def test_arrancar_sin_el_parametro_nuevo_no_toca_el_autoplay(cog):
    vc = FakePlayer(auto=[FakeTrack("sugerida")])
    await cog._arrancar_si_hace_falta(vc)
    assert vc.tocando == [] and len(vc.auto_queue) == 1

async def test_con_cola_el_autoplay_se_queda_quieto_aunque_se_lo_incluya(cog):
    pedida, sugerida = FakeTrack("pedida"), FakeTrack("sugerida")
    vc = FakePlayer(temas=[pedida], auto=[sugerida])
    await cog._arrancar_si_hace_falta(vc, incluir_autoplay=True)
    assert vc.tocando == [pedida] and list(vc.auto_queue) == [sugerida]

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
    assert presentados == [sugerida] and vc.tocando == [corte] and list(vc.auto_queue) == [sugerida]

async def test_cog_load_arranca_vigilante_y_guardador_y_cog_unload_los_cancela_y_cierra_el_resolver(cog, lifecycle):
    cerrado = []
    async def _cerrar():
        cerrado.append(True)

    cog.resolver.cerrar = _cerrar
    await cog.cog_load()
    tareas = cog._vigilante, cog._guardador
    assert all(t is not None and not t.done() for t in tareas) and cog._guardador in cog._tareas
    await cog.cog_unload()
    await asyncio.wait_for(asyncio.gather(*tareas, return_exceptions=True), timeout=1)
    assert all(t.cancelled() for t in tareas) and cerrado == [True] and cog._vista is None

async def test_un_error_en_la_revision_no_mata_el_bucle(cog, ticks, monkeypatch, caplog):
    revisiones = []
    async def _revision(players):
        revisiones.append(players)
        if len(revisiones) == 1:
            raise RuntimeError("se cayó algo")

    monkeypatch.setattr(cog, "_vigilar_una_vez", _revision)
    with caplog.at_level("ERROR", logger="music"):
        bucle = asyncio.create_task(cog._vigilar())
        for _ in range(10):
            await ticks(0)
        bucle.cancel()
        await asyncio.gather(bucle, return_exceptions=True)
    assert len(revisiones) > 1 and "Falló una revisión" in caplog.text

def test_el_vigilante_solo_mira_los_players_de_wavelink(cog):
    assert cog._players_del_bot() == []
    player = wavelink.Player.__new__(wavelink.Player)
    cog.bot = SimpleNamespace(voice_clients=[player, object()])
    assert cog._players_del_bot() == [player]

def _con_reporte_de_lavalink(vc, posicion):
    vc.reanudar = {}
    vc._last_update, vc._inicio_ns, vc._last_position = 2, 1, posicion
    return vc

async def test_un_reintento_a_mitad_de_tema_reanuda_desde_el_corte_menos_un_respiro(cog):
    vc = _con_reporte_de_lavalink(_con_canal(), 60_000)
    pista = FakeTrack("la que se cortó")
    await _falla(cog, vc, pista)
    assert vc.reanudar == {"la que se cortó": 58_000} and list(vc.queue) == [pista]

@pytest.mark.parametrize("posicion, viejo", [(5_000, False), (165_000, False), (60_000, True)],
                         ids=["recien-empezada", "casi-terminada", "reporte-del-tema-anterior"])
async def test_un_corte_sin_reporte_propio_o_muy_temprano_o_tardio_no_se_reanuda(cog, posicion, viejo):
    vc = _con_reporte_de_lavalink(_con_canal(), posicion)
    if viejo:
        vc._last_update, vc._inicio_ns = 1, 2
    await _falla(cog, vc, FakeTrack("la que se cortó"))
    assert vc.reanudar == {}

async def test_un_player_sin_tabla_de_reanudacion_igual_reintenta(cog):
    vc = _con_canal()
    vc._last_update, vc._inicio_ns, vc._last_position = 2, 1, 60_000
    assert not hasattr(vc, "reanudar")
    pista = FakeTrack("la que se cortó")
    await _falla(cog, vc, pista)
    assert list(vc.queue) == [pista]
# --- listas de Spotify --------------------------------------------------------------------------

def _con_lista_de_spotify(monkeypatch, *consultas, nombre="una lista"):
    async def _fetch(session, url, limit=100):
        return Lista(kind="playlist", name=nombre, queries=list(consultas))
    monkeypatch.setattr(music_mod.spotify, "fetch", _fetch)

async def test_un_link_de_spotify_de_un_solo_tema_suena_al_toque(cog, monkeypatch):
    vc = FakePlayer()
    ctx = FakeCtx(vc)
    cog._session = object()
    _busca(monkeypatch)
    _con_lista_de_spotify(monkeypatch, "Artista - Tema")
    await cog._play_spotify(ctx, vc, "https://open.spotify.com/track/x")
    assert [t.title for t in vc.tocando] == ["Artista - Tema"] and vc.tocando[0].requester == "ana"
    assert ctx.ultimo == "🎧 Spotify — **Artista - Tema**" and cog.estacion(ctx.guild.id).spotify is None

async def test_una_lista_de_spotify_arranca_con_el_primero_y_carga_el_resto_de_fondo(cog, monkeypatch):
    vc = FakePlayer()
    ctx = FakeCtx(vc)
    cog._session = object()
    _busca(monkeypatch, sin=("c",))
    _con_lista_de_spotify(monkeypatch, "a", "b", "c", "d")
    await cog._play_spotify(ctx, vc, "https://open.spotify.com/playlist/x")
    assert [t.title for t in vc.tocando] == ["a"] and "buscando 4 temas" in ctx.enviados[-1]
    carga = cog.estacion(ctx.guild.id).spotify
    assert carga is not None
    await carga
    assert [t.title for t in vc.queue] == ["b", "d"]
    assert ctx.ultimo == "🎧 Listo: 2 temas más en la cola. No encontré 1 en YouTube."
    assert cog.estacion(ctx.guild.id).spotify is None

@pytest.mark.parametrize("propia", [True, False], ids=["la-propia", "la-de-una-lista-nueva"])
async def test_si_el_player_se_va_mientras_carga_la_lista_no_hay_resumen_y_solo_suelta_su_tarea(cog, monkeypatch, propia):
    vc = FakePlayer()
    vc.connected = False
    ctx = FakeCtx(vc)
    _busca(monkeypatch)
    est = cog.estacion(ctx.guild.id)
    est.spotify = ref = asyncio.current_task() if propia else object()
    await cog._encolar_spotify(ctx, vc, ["a", "b"], "ana")
    assert ctx.enviados == [] and est.spotify is (None if propia else ref)

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
    assert tarea.cancelled() and est.spotify is None and ctx.enviados == []
# --- la cola en disco ----------------------------------------------------------------------------

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

class DiscordFalso:
    def __init__(self):
        self.guilds, self.listo, self.voice_clients = {}, False, []
    def servidor(self, guild_id, voz=10, texto=20, humanos=1):
        guild = self.guilds[guild_id] = FakeGuild(guild_id)
        guild.canales[voz] = FakeCanalDeVoz(voz, guild, humanos)
        guild.canales[texto] = FakeCanalTexto()
        guild.canales[texto].id = texto
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
def escrituras(monkeypatch):
    hechas, original = [], persistencia.guardar
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
    vc = PlayerRetomable(FakeGuild(guild_id))
    vc.channel, vc.text_channel = SimpleNamespace(id=10), SimpleNamespace(id=20)
    vc.current = wavelink.Playable(_raw(actual)) if actual else None
    vc.queue.extend(wavelink.Playable(_raw(t)) for t in cola)
    vc.guild.voice_client = vc
    return vc

@pytest.fixture
def jugadores(cog):
    class Conectados(list):
        def nuevo(self, *args, **kwargs):
            self.append(_player_con_cola(*args, **kwargs))
            return self[-1]

    lista = Conectados()
    cog._players_del_bot = lambda: list(lista)
    return lista

def _en_disco():
    return persistencia.cargar()

def _titulos_en_disco(guild_id=1):
    return [p["info"]["title"] for p in _en_disco()[guild_id]["cola"]]

def test_una_pista_guardada_conserva_quien_la_pidio():
    pista = Music._pista({**_raw("tema"), "pedido_por": "ana"})
    assert pista.requester == "ana" and pista.title == "tema"

async def test_solo_escribe_cuando_algo_cambio_o_pasa_el_refresco(jugadores, cog, escrituras):
    vc = jugadores.nuevo()
    cog._guardar(siempre=False)
    cog._guardar(siempre=False)
    assert len(escrituras) == 1
    vc._last_update, vc._inicio_ns, vc._last_position = 2, 1, 40_000     # avanzar el tema no cuenta
    cog._guardar(siempre=False)
    assert len(escrituras) == 1
    vc.queue.append(wavelink.Playable(_raw("tres")))
    cog._guardar(siempre=False)
    cog._guardar(siempre=False)
    assert len(escrituras) == 2 and _titulos_en_disco() == ["uno", "dos", "tres"]
    vc.current = vc.queue.get()      # cambiar de tema sí
    cog._guardar(siempre=False)
    assert len(escrituras) == 3
    cog._escrita -= music_mod.REFRESCAR_CADA
    cog._guardar(siempre=False)
    assert len(escrituras) == 4

@pytest.mark.parametrize("cambio", [
    lambda vc, cog: setattr(vc, "paused", True),
    lambda vc, cog: setattr(vc, "volume", 30),
    lambda vc, cog: setattr(vc.queue, "mode", wavelink.QueueMode.loop),
    lambda vc, cog: cog._autoplay.__setitem__(1, not cog.autoplay_activo(1)),
], ids=["pausa", "volumen", "modo", "autoplay"])
async def test_pausar_o_cambiar_volumen_modo_o_autoplay_cuenta_como_cambio(jugadores, cog, escrituras, cambio):
    vc = jugadores.nuevo()
    cog._guardar(siempre=False)
    cambio(vc, cog)
    cog._guardar(siempre=False)
    assert len(escrituras) == 2

async def test_los_cortes_del_dj_no_van_al_archivo(jugadores, cog, tmp_path, monkeypatch):
    monkeypatch.setattr(music_mod.tts, "CACHE_DIR", tmp_path)
    vc = jugadores.nuevo(cola=())
    vc.queue.append(wavelink.Playable(_raw("corte", uri=str(tmp_path / "corte.wav"))))
    vc.queue.append(wavelink.Playable(_raw("tema")))
    cog._guardar(siempre=False)
    assert _titulos_en_disco() == ["tema"]

async def test_un_servidor_que_no_se_puede_fotografiar_no_frena_a_los_demas(jugadores, cog, caplog):
    jugadores.nuevo(guild_id=1).queue.mode = None
    jugadores.nuevo(guild_id=2)
    with caplog.at_level("ERROR", logger="music"):
        cog._guardar(siempre=False)
    assert list(_en_disco()) == [2] and "No pude fotografiar la cola" in caplog.text

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
    fallos, original = [], persistencia.guardar
    def _guardar(fotos, *args, **kwargs):
        if not fallos:
            fallos.append(1)
            raise OSError("disco lleno")
        return original(fotos, *args, **kwargs)

    monkeypatch.setattr(persistencia, "guardar", _guardar)
    with caplog.at_level("ERROR", logger="music"):
        await _correr_ticks(cog, ticks)
    assert "No pude guardar las colas" in caplog.text and list(_en_disco()) == [1]

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
# --- teardown y guardar_y_cerrar -----------------------------------------------------------------

@pytest.mark.parametrize("con_player", [True, False], ids=["con-player", "sin-player"])
async def test_teardown_borra_la_foto_de_ese_servidor(cog, con_player):
    persistencia.guardar({1: _foto_guardada(), 2: _foto_guardada()})
    guild = PlayerDeSesionVieja(guild=FakeGuild(1)).guild if con_player else FakeGuild(1)
    await cog.teardown(guild)
    assert list(_en_disco()) == [2]

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

async def test_guardar_y_cerrar_escribe_la_foto_final_una_sola_vez_aunque_nada_haya_cambiado(jugadores, cog, escrituras):
    vc = jugadores.nuevo()
    cog._persistencia = "activa"
    cog._guardar(siempre=False)
    vc._last_update, vc._inicio_ns, vc._last_position = 2, 1, 61_000
    await cog.guardar_y_cerrar()
    await cog.guardar_y_cerrar()
    assert len(escrituras) == 2 and _en_disco()[1]["posicion"] == 61_000

async def test_guardar_y_cerrar_frena_la_tarea_y_ya_no_se_escribe(jugadores, cog, ticks, escrituras):
    vc = jugadores.nuevo()
    cog._persistencia = "activa"
    guardador = cog._guardador = cog._segundo_plano(cog._guardar_en_bucle())
    await ticks(0)
    await cog.guardar_y_cerrar()
    assert guardador.cancelled() and cog._guardador is None and cog._persistencia == "cerrada"
    escritas = len(escrituras)
    vc.connected = False     # sin players, otro tick guardaría una cola vacía encima de la buena
    await _correr_ticks(cog, ticks)
    assert len(escrituras) == escritas and list(_en_disco()) == [1]

async def test_guardar_y_cerrar_antes_de_retomar_no_pisa_lo_guardado(cog):
    persistencia.guardar({7: _foto_guardada()})
    await cog.guardar_y_cerrar()
    assert list(_en_disco()) == [7] and cog._persistencia == "cerrada"

async def test_guardar_y_cerrar_con_el_disco_roto_no_levanta_nada(jugadores, cog, monkeypatch, caplog):
    jugadores.nuevo()
    cog._persistencia = "activa"
    def _falla(*args, **kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr(persistencia, "guardar", _falla)
    with caplog.at_level("ERROR", logger="music"):
        await cog.guardar_y_cerrar()
    assert cog._persistencia == "cerrada" and "No pude guardar la foto final" in caplog.text

async def test_cerrar_a_mitad_de_retomar_no_reactiva_el_guardado(cog):
    persistencia.guardar({7: _foto_guardada()})

    class BotQueCierra(DiscordFalso):
        async def wait_until_ready(self):
            await cog.guardar_y_cerrar()

    cog.bot = BotQueCierra()
    await cog.retomar_guardadas()
    assert cog._persistencia == "cerrada"
# --- retomar_guardadas ---------------------------------------------------------------------------

async def test_retomar_conecta_carga_la_cola_y_toca_desde_la_posicion(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada()})
    await cog.retomar_guardadas()
    canal, texto = guild.canales[10], guild.canales[20]
    vc = canal.player
    assert discord_falso.listo and canal.clase is music_mod.resolver.ResolvingPlayer
    assert vc.resolver is cog.resolver and vc.text_channel is texto
    assert [p.title for p in vc.queue] == ["uno", "dos"]
    [(actual, kwargs)] = vc.llamadas
    assert actual.title == "sonando" and kwargs == {"start": 95_000, "paused": True, "add_history": False}
    assert vc.volume == 55 and vc.queue.mode is wavelink.QueueMode.loop
    assert cog.autoplay_activo(1) is False and vc.autoplay is wavelink.AutoPlayMode.partial
    assert texto.enviados == [music_mod.AVISO_RETOMADA] and cog._persistencia == "activa"

async def test_retomar_respeta_la_lista_blanca(cog, discord_falso, monkeypatch):
    uno, dos = discord_falso.servidor(1), discord_falso.servidor(2)
    persistencia.guardar({1: _foto_guardada(), 2: _foto_guardada()})
    monkeypatch.setattr(music_mod.config, "SERVIDORES_PERMITIDOS", frozenset({2}))
    await cog.retomar_guardadas()
    assert uno.canales[10].conexiones == 0 and dos.canales[10].conexiones == 1

async def test_retomar_se_salta_el_servidor_donde_el_bot_ya_no_esta(cog, discord_falso):
    otro = discord_falso.servidor(2)
    persistencia.guardar({1: _foto_guardada(), 2: _foto_guardada()})
    await cog.retomar_guardadas()
    assert otro.canales[10].conexiones == 1

def _sin_humanos(guild, n=1):
    guild.canales[10].members = [SimpleNamespace(bot=True)] * n

@pytest.mark.parametrize("foto, preparar", [
    ({"hora": time.time() - persistencia.VIGENCIA - 60}, None),
    ({"voz": 999}, None),
    ({}, lambda guild: setattr(guild, "voice_client", object())),
    ({}, _sin_humanos),
    ({}, lambda guild: _sin_humanos(guild, 2)),
], ids=["foto-vencida", "canal-inexistente", "transmision-existente", "sin-humanos", "solo-bots"])
async def test_retomar_no_entra_cuando_no_corresponde(cog, discord_falso, caplog, foto, preparar):
    guild = discord_falso.servidor(1)
    if preparar:
        preparar(guild)
    persistencia.guardar({1: _foto_guardada(**foto)})
    with caplog.at_level("INFO", logger="music"):
        await cog.retomar_guardadas()
    assert guild.canales[10].conexiones == 0
    if guild.canales[10].members[0].bot and foto == {}:
        assert "no hay nadie en el canal" in caplog.text

@pytest.mark.parametrize("falla", ["falla_al_conectar", "falla_al_tocar"])
async def test_un_servidor_que_falla_al_retomar_no_frena_a_los_otros(cog, discord_falso, caplog, falla):
    roto, bueno = discord_falso.servidor(1), discord_falso.servidor(2)
    setattr(roto.canales[10], falla, True)
    persistencia.guardar({1: _foto_guardada(), 2: _foto_guardada()})
    with caplog.at_level("ERROR", logger="music"):
        await cog.retomar_guardadas()
    assert bueno.canales[10].player.llamadas and "No pude retomar la cola de 1" in caplog.text
    assert cog._persistencia == "activa"
    if falla == "falla_al_tocar":
        assert not roto.canales[10].player.connected

async def test_retomar_solo_corre_una_vez(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada()})
    await cog.retomar_guardadas()
    guild.voice_client = None
    await cog.retomar_guardadas()
    assert guild.canales[10].conexiones == 1

@pytest.mark.parametrize("contenido", [None, "{roto"], ids=["sin-archivo", "archivo-roto"])
async def test_sin_nada_que_retomar_el_guardado_queda_activo(cog, discord_falso, archivo_de_colas, contenido):
    if contenido:
        archivo_de_colas.parent.mkdir(parents=True)
        archivo_de_colas.write_text(contenido)
    await cog.retomar_guardadas()
    assert cog._persistencia == "activa"

async def test_sin_tema_en_curso_empieza_por_la_cola(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada(actual=None, posicion=0)})
    await cog.retomar_guardadas()
    vc = guild.canales[10].player
    [(primero, kwargs)] = vc.llamadas
    assert primero.title == "uno" and kwargs == {} and [p.title for p in vc.queue] == ["dos"]

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
    assert not guild.canales[10].player.connected and "no quedó nada que tocar" in caplog.text

async def test_sin_canal_de_texto_retoma_igual_y_no_avisa_a_nadie(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada(texto=None)})
    await cog.retomar_guardadas()
    vc = guild.canales[10].player
    assert vc.text_channel is None and vc.llamadas and guild.canales[20].enviados == []

async def test_si_el_aviso_no_sale_la_cola_sigue_sonando(cog, discord_falso):
    guild = discord_falso.servidor(1)
    persistencia.guardar({1: _foto_guardada()})
    async def _prohibido(contenido=None, **kwargs):
        raise music_mod.discord.Forbidden(SimpleNamespace(status=403, reason="x"), "sin permiso")

    guild.canales[20].send = _prohibido
    await cog.retomar_guardadas()
    vc = guild.canales[10].player
    assert vc.connected and vc.llamadas
# --- saltar las partes que no son música ---------------------------------------------------------

async def _sin_precarga(self, vc, est, pista):
    return None

class Escenario:
    """Un tema sonando con reloj, posición y reportes de Lavalink simulados (uno cada 5 s, solo mientras suena)."""
    def __init__(self, monkeypatch, vc, *, precarga=False):
        self.vc, self.ns, self.t, self.pos, self.reporte, self.eventos = vc, 10**12, 0.0, 0, 0.0, []
        self.pedidas = []   # (pista, reloj, posición) de cada precarga que le piden al resolver
        vc.escenario = self
        original = asyncio.sleep
        monkeypatch.setattr(music_mod, "time", SimpleNamespace(monotonic_ns=lambda: self.ns, monotonic=time.monotonic))
        if not precarga:    # su espera también corre este reloj y lo adelantaría el doble
            monkeypatch.setattr(Music, "_precargar_al_final", _sin_precarga)
        async def dormir(segundos, *a):
            self.avanzar(segundos)
            await original(0)

        monkeypatch.setattr(music_mod.asyncio, "sleep", dormir)
    def a_los(self, segundos, accion):
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
    def __init__(self, pista):
        super().__init__(playing=True)
        self.current, self.text_channel = pista, FakeCanalTexto()
        self.seeks, self.skips, self.falla_el_seek = [], [], False
    async def seek(self, ms):
        if self.falla_el_seek:
            raise RuntimeError("Lavalink dijo que no")
        self.seeks.append((ms, self.escenario.pos))
        self.escenario.pos = ms
    async def skip(self, force=True):
        self.skips.append(self.escenario.pos)

async def _arrancar(cog, monkeypatch, segmentos, *, arranque=0, pista=None, precarga=False, cola=()):
    """Arranca un tema de 200 s con esos segmentos ya en la caché y devuelve (player, escenario, estación, pista)."""
    pista = pista or _tema()
    vc = PlayerQueSalta(pista)
    vc.queue.extend(cola)
    sb = SponsorBlock(activo=True)
    sb._cache["abc"] = tuple(segmentos)
    vc.resolver = Resolver(sponsorblock=sb)
    vc.arranque_ms = arranque
    escenario = Escenario(monkeypatch, vc, precarga=precarga)
    escenario.pos = arranque
    vc.resolver.precargar = lambda p: escenario.pedidas.append((p, escenario.t, escenario.pos))
    await _inicio(cog, vc, pista)
    return vc, escenario, cog.estacion(vc.guild.id), pista

async def _terminar(est):
    await asyncio.wait_for(asyncio.gather(*list(est.segmentos), return_exceptions=True), timeout=5)

async def _precargada(est):
    if tarea := est.precarga:
        await asyncio.wait_for(tarea, timeout=5)

def _pedidas(escenario):
    return [pista for pista, _, _ in escenario.pedidas]

def _lineas_de_salto(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "music" and "no es música" in r.getMessage()]

@pytest.mark.parametrize("inicio, fin", [(60_000, 75_000), (0, 12_500)], ids=["del-medio", "intro-que-llego-tarde"])
async def test_una_parte_se_salta_con_seek_a_su_final(cog, sin_panel, monkeypatch, caplog, inicio, fin):
    with caplog.at_level("INFO", logger="music"):
        vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(inicio, fin, 200_000)])
        assert len(est.segmentos) == 1
        await _terminar(est)
    [(destino, donde)] = vc.seeks
    assert destino == fin and inicio <= donde <= inicio + 2_500
    assert vc.skips == [] and est.segmentos == []
    [linea] = _lineas_de_salto(caplog)
    assert "abc" in linea and str(inicio // 1000) in linea and str(fin // 1000) in linea

@pytest.mark.parametrize("fin, salta", [(195_000, True), (198_000, True), (200_000, True), (190_000, False)])
async def test_la_parte_que_llega_al_final_salta_al_tema_siguiente_y_la_que_no_solo_hace_seek(
    cog, sin_panel, monkeypatch, caplog, fin, salta
):
    with caplog.at_level("INFO", logger="music"):
        vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(170_000, fin, 200_000)])
        await _terminar(est)
    if salta:
        [donde] = vc.skips
        assert 170_000 <= donde <= 172_500 and vc.seeks == [] and len(_lineas_de_salto(caplog)) == 1
    else:
        assert [d for d, _ in vc.seeks] == [fin] and vc.skips == []

async def test_cada_parte_tiene_su_vigia_y_se_saltan_en_orden(cog, sin_panel, monkeypatch):
    partes = [Segmento(30_000, 40_000, 200_000), Segmento(100_000, 110_000, 200_000), Segmento(180_000, 200_000, 200_000)]
    vc, _, est, _ = await _arrancar(cog, monkeypatch, partes)
    assert len(est.segmentos) == 3
    await _terminar(est)
    assert [d for d, _ in vc.seeks] == [40_000, 110_000]
    assert len(vc.skips) == 1 and 180_000 <= vc.skips[0] <= 186_000 and est.segmentos == []

async def test_una_pausa_del_usuario_corre_los_tiempos(cog, sin_panel, monkeypatch):
    """Pausa de 30 s a los 50: dormir 60 s desde el arranque saltaría 10 s de canción."""
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)])
    escenario.a_los(50, lambda: setattr(vc, "paused", True))
    escenario.a_los(80, lambda: setattr(vc, "paused", False))
    await _terminar(est)
    [(destino, donde)] = vc.seeks
    assert destino == 70_000 and 60_000 <= donde <= 62_500 and escenario.t >= 89

async def test_un_seek_del_usuario_hacia_adelante_se_nota_y_nunca_se_vuelve_atras(cog, sin_panel, monkeypatch):
    partes = [Segmento(60_000, 70_000, 200_000), Segmento(100_000, 110_000, 200_000)]
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, partes)
    escenario.a_los(10, lambda: setattr(escenario, "pos", 95_000))
    await _terminar(est)
    [(destino, donde)] = vc.seeks
    assert destino == 110_000 and 100_000 <= donde <= 107_000

@pytest.mark.parametrize("pasa", ["otra pista", "desconectado"])
async def test_si_la_pista_se_fue_o_el_player_se_desconecto_no_salta_ni_precarga(cog, sin_panel, monkeypatch, pasa):
    vc, escenario, est, _ = await _arrancar(
        cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)], precarga=True, cola=[FakeTrack("la que sigue")]
    )
    if pasa == "otra pista":
        escenario.a_los(20, lambda: setattr(vc, "current", _tema("otra")))
    else:
        escenario.a_los(20, lambda: setattr(vc, "connected", False))
    await _terminar(est)
    await _precargada(est)
    assert vc.seeks == [] and vc.skips == [] and est.segmentos == []
    assert escenario.pedidas == [] and est.precarga is None

@pytest.mark.parametrize("parte, arranque", [
    (Segmento(0, 12_500, 200_000), 12_500), (Segmento(60_000, 70_000, 200_000), 80_000),
], ids=["intro-ya-saltada", "reintento-pasada-la-parte"])
async def test_una_parte_que_ya_quedo_atras_no_se_salta(cog, sin_panel, monkeypatch, parte, arranque):
    vc, _, est, _ = await _arrancar(cog, monkeypatch, [parte], arranque=arranque)
    await _terminar(est)
    assert vc.seeks == [] and vc.skips == []

async def test_si_el_seek_falla_se_avisa_y_no_escapa_ninguna_excepcion(cog, sin_panel, monkeypatch, caplog):
    vc, _, est, _ = await _arrancar(cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)])
    vc.falla_el_seek = True
    tareas = list(est.segmentos)
    with caplog.at_level("WARNING", logger="music"):
        resultados = await asyncio.wait_for(asyncio.gather(*tareas, return_exceptions=True), timeout=5)
    assert resultados == [None] and "No pude saltar" in caplog.text

async def test_al_terminar_la_cancion_se_cancelan_los_vigias_y_la_precarga(cog, sin_panel, monkeypatch):
    vc, escenario, est, pista = await _arrancar(
        cog, monkeypatch, [Segmento(60_000, 70_000, 200_000)], precarga=True, cola=[FakeTrack("la que sigue")]
    )
    tareas = [*est.segmentos, est.precarga]
    assert len(tareas) == 2
    await _fin(cog, vc, pista, "replaced")
    await asyncio.gather(*tareas, return_exceptions=True)
    assert all(t.cancelled() for t in tareas)
    assert est.segmentos == [] and vc.seeks == [] and est.precarga is None and escenario.pedidas == []

async def test_el_final_de_un_servidor_sin_estacion_no_la_crea(cog):
    vc = FakePlayer()
    vc.queue.append(FakeTrack("la que sigue"))
    await _fin(cog, vc, FakeTrack("la que terminó"), "replaced")
    assert cog._estaciones == {}

async def test_el_vigia_que_ya_esta_actuando_no_se_cancela_con_el_final_que_provoca(cog, sin_panel, monkeypatch):
    """El skip de un vigía dispara el `track_end` del tema: no puede cortarle el skip a medias."""
    vc, _, est, pista = await _arrancar(cog, monkeypatch, [Segmento(180_000, 200_000, 200_000)])
    vc.queue.append(FakeTrack("la que sigue"))
    [vigia] = est.segmentos
    skip = vc.skip
    async def _skip_con_final(force=True):
        await skip(force)
        await _fin(cog, vc, pista, "replaced")

    vc.skip = _skip_con_final
    await asyncio.wait_for(vigia, timeout=5)
    assert not vigia.cancelled() and len(vc.skips) == 1

@pytest.mark.parametrize("clip, segmentos", [(True, [Segmento(60_000, 70_000, 200_000)]), (False, [])],
                         ids=["corte-del-dj", "sin-marcas"])
async def test_sin_nada_que_saltar_no_se_programa_nada(cog, sin_panel, monkeypatch, clip, segmentos):
    pista = _tema()
    pista.is_dj_clip = clip
    _, _, est, _ = await _arrancar(cog, monkeypatch, segmentos, pista=pista)
    assert est.segmentos == []

async def test_con_sponsorblock_apagado_no_se_programa_ni_se_consulta_nada(cog, sin_panel):
    pista = _tema()
    vc = PlayerQueSalta(pista)
    vc.resolver = Resolver()
    assert vc.resolver.sponsorblock.activo is False
    await _inicio(cog, vc, pista)
    assert cog.estacion(1).segmentos == []
    assert vc.resolver.sponsorblock._http is None and vc.resolver.sponsorblock._en_curso == {}

async def test_si_el_resolver_no_sabe_de_segmentos_el_panel_sale_igual(cog, sin_panel, caplog):
    pista = _tema()
    vc = PlayerQueSalta(pista)
    vc.resolver = ResolverQueAnota()
    with caplog.at_level("WARNING", logger="music"):
        await _inicio(cog, vc, pista)
    assert sin_panel == [vc] and cog.estacion(1).segmentos == [] and "partes que no son música" in caplog.text

BASE_POSICION = dict(_last_update=5 * 10**9, _last_position=50_000, _inicio_ns=10**9, arranque_ms=0, paused=False)

@pytest.mark.parametrize("ahora_s, cambios, esperado", [
    (8, {}, 53_000), (11, {}, 56_000), (12, {}, 50_000), (305, {}, 50_000), (8, {"paused": True}, 50_000),
    (4, {"_last_update": None, "arranque_ms": 12_500}, 15_500),
    (4, {"_last_update": 5 * 10**8, "_last_position": 170_000}, 3_000),
], ids=["reciente", "al-limite", "viejo", "muy-viejo", "en-pausa", "antes-del-primer-reporte", "reporte-del-tema-anterior"])
def test_la_posicion_solo_se_extrapola_de_un_reporte_reciente_del_tema_y_sin_pausa(monkeypatch, ahora_s, cambios, esperado):
    monkeypatch.setattr(music_mod, "time", SimpleNamespace(monotonic_ns=lambda: ahora_s * 10**9))
    assert Music.posicion_viva(SimpleNamespace(**{**BASE_POSICION, **cambios})) == esperado
# --- precargar la URL del tema que viene, justo antes del final ---------------------------------

async def test_una_cancion_larga_no_precarga_al_arrancar_sino_en_sus_ultimos_30_segundos(cog, sin_panel, monkeypatch):
    siguiente = FakeTrack("la que sigue")
    _, escenario, est, _ = await _arrancar(
        cog, monkeypatch, [], precarga=True, cola=[siguiente, FakeTrack("la de después")]
    )
    assert escenario.pedidas == [] and est.precarga is not None
    await _precargada(est)
    [(pista, _, posicion)] = escenario.pedidas     # solo la próxima: la URL de las demás caducaría
    limite = 200_000 - music_mod.PRECARGAR_ANTES * 1000
    assert pista is siguiente and limite <= posicion <= limite + 6_000 and est.precarga is None

@pytest.mark.parametrize("hay_autoplay", [True, False], ids=["la-del-autoplay", "nada-que-venga"])
async def test_con_la_cola_vacia_se_precarga_la_del_autoplay_si_la_hay(cog, sin_panel, monkeypatch, hay_autoplay):
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, [], precarga=True)
    sugerida = FakeTrack("la sugerida")
    if hay_autoplay:
        vc.auto_queue.append(sugerida)
    await _precargada(est)
    assert _pedidas(escenario) == ([sugerida] if hay_autoplay else []) and est.precarga is None

async def test_lo_que_viene_se_elige_al_momento_de_precargar_no_al_arrancar(cog, sin_panel, monkeypatch):
    nueva = FakeTrack("la que pusieron después")
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, [], precarga=True, cola=[FakeTrack("la que había")])
    escenario.a_los(60, lambda: vc.queue.insert(0, nueva))
    await _precargada(est)
    assert _pedidas(escenario) == [nueva]

async def test_una_pausa_demora_la_precarga(cog, sin_panel, monkeypatch):
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, [], precarga=True, cola=[FakeTrack("la que sigue")])
    escenario.a_los(50, lambda: setattr(vc, "paused", True))
    escenario.a_los(80, lambda: setattr(vc, "paused", False))
    await _precargada(est)
    [(_, reloj, posicion)] = escenario.pedidas
    assert reloj >= 199 and 170_000 <= posicion <= 176_000

async def test_un_salto_adelante_del_usuario_adelanta_la_precarga(cog, sin_panel, monkeypatch):
    _, escenario, est, _ = await _arrancar(cog, monkeypatch, [], precarga=True, cola=[FakeTrack("la que sigue")])
    escenario.a_los(10, lambda: setattr(escenario, "pos", 150_000))
    await _precargada(est)
    [(_, reloj, posicion)] = escenario.pedidas
    assert reloj < 40 and posicion >= 170_000

@pytest.mark.parametrize("parte", [Segmento(150_000, 200_000, 200_000), Segmento(150_000, 196_000, 200_000)])
async def test_una_parte_final_que_no_es_musica_adelanta_el_final_del_tema(cog, sin_panel, monkeypatch, parte):
    """Ahí el cog salta al siguiente: contar hasta el largo precargaría tarde."""
    siguiente = FakeTrack("la que sigue")
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, [parte], precarga=True, cola=[siguiente])
    await _precargada(est)
    await _terminar(est)
    [(pista, _, posicion)] = escenario.pedidas
    assert pista is siguiente and 120_000 <= posicion < 150_000 and len(vc.skips) == 1

@pytest.mark.parametrize("parte", [Segmento(40_000, 60_000, 200_000), Segmento(150_000, 190_000, 200_000)])
async def test_una_parte_que_no_llega_al_final_no_mueve_la_precarga(cog, sin_panel, monkeypatch, parte):
    _, escenario, est, _ = await _arrancar(cog, monkeypatch, [parte], precarga=True, cola=[FakeTrack("la que sigue")])
    await _precargada(est)
    [(_, _, posicion)] = escenario.pedidas
    assert posicion >= 170_000

async def test_si_no_se_pueden_leer_las_marcas_se_cuenta_el_largo_de_la_pista(cog, sin_panel, monkeypatch):
    def _revienta(self, pista):
        raise RuntimeError("caché rota")

    monkeypatch.setattr(Resolver, "segmentos_de", _revienta)
    _, escenario, est, _ = await _arrancar(cog, monkeypatch, [], precarga=True, cola=[FakeTrack("la que sigue")])
    await _precargada(est)
    [(_, _, posicion)] = escenario.pedidas
    assert 170_000 <= posicion <= 176_000

@pytest.mark.parametrize("largo, arranque, stream", [
    (20_000, 0, False), (30_000, 0, False), (0, 0, False), (200_000, 175_000, False), (2**63 - 1, 0, True),
], ids=["corto", "justo-30s", "sin-largo", "quedan-30s", "stream"])
async def test_un_tema_corto_sin_largo_stream_o_al_que_ya_le_quedan_30_segundos_precarga_enseguida(
    cog, sin_panel, monkeypatch, largo, arranque, stream
):
    siguiente = FakeTrack("la que sigue")
    pista = _tema(largo=largo)
    pista.is_stream = stream
    _, escenario, est, _ = await _arrancar(cog, monkeypatch, [], pista=pista, arranque=arranque, precarga=True, cola=[siguiente])
    await _precargada(est)
    assert [(p, reloj) for p, reloj, _ in escenario.pedidas] == [(siguiente, 0.0)]

@pytest.mark.parametrize("otro", ["una canción", "un corte del DJ"])
async def test_arrancar_otro_tema_cancela_la_precarga_del_anterior(cog, sin_panel, monkeypatch, otro):
    siguiente = FakeTrack("la que sigue")
    vc, escenario, est, _ = await _arrancar(cog, monkeypatch, [], precarga=True, cola=[siguiente])
    anterior = est.precarga
    nuevo = _tema("otra")
    nuevo.is_dj_clip = otro == "un corte del DJ"
    vc.current = nuevo
    await _inicio(cog, vc, nuevo)
    await asyncio.gather(anterior, return_exceptions=True)
    await _precargada(est)
    assert anterior.cancelled() and est.precarga is None
    assert _pedidas(escenario) == [siguiente]   # una sola vez: la del anterior no llegó a pedir nada

async def test_un_error_dentro_de_la_tarea_se_anota_y_no_escapa(cog, sin_panel, monkeypatch, caplog):
    _, escenario, est, _ = await _arrancar(cog, monkeypatch, [], precarga=True, cola=[FakeTrack("la que sigue")])
    tarea = est.precarga
    def _sin_posicion(vc):
        raise RuntimeError("sin posición")

    cog.posicion_viva = _sin_posicion
    with caplog.at_level("WARNING", logger="music"):
        assert await asyncio.wait_for(tarea, timeout=5) is None
    assert "No pude precargar a tiempo" in caplog.text and "sin posición" in caplog.text
    assert escenario.pedidas == [] and est.precarga is None
# --- comandos híbridos: `!` y `/` ----------------------------------------------------------------

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
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    await bot.add_cog(Music(bot))
    yield bot
    await bot.remove_cog("Music")

def _ctx_de_barra(bot, vc=None, author=None):
    ctx = FakeCtx(vc, author=author)
    ctx.interaction = SimpleNamespace(client=bot)
    ctx.interaction._baton = ctx
    return ctx

def test_los_comandos_de_musica_son_hibridos_menos_search():
    comandos = {c.name: c for c in Music.__cog_commands__}
    assert {n for n, c in comandos.items() if isinstance(c, commands.HybridCommand)} == HIBRIDOS
    assert set(comandos) == HIBRIDOS | {"search"}     # `search` elige con reacciones: solo prefijo

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
    _busca(monkeypatch, [FakeTrack("única")])
    for comando in ("play", "playnext"):
        vc = FakePlayer(playing=False)
        ctx = _conectar(monkeypatch, vc, interaction=interaccion)
        await getattr(cog, comando)(cog, ctx, query="única")
        assert [t.title for t in vc.tocando] == ["única"]
        assert ctx.enviados == (["▶️ **única**"] if avisa else [])

@pytest.mark.parametrize("interaccion, avisa", [(object(), True), (None, False)])
async def test_con_barra_el_panel_responde_aunque_se_publique_en_el_canal(cog, sin_panel, interaccion, avisa):
    vc = FakePlayer(playing=True)
    vc.current = FakeTrack()
    ctx = FakeCtx(vc, interaction=interaccion)
    await cog.panel_cmd(cog, ctx)
    assert sin_panel == [vc] and ctx.enviados == (["Panel publicado."] if avisa else [])

async def test_con_barra_los_checks_siguen_negando_lo_privado_y_lo_ajeno(bot_con_musica):
    privado = _ctx_de_barra(bot_con_musica)
    privado.guild = None
    for nombre in ("play", "queue", "skip"):
        with pytest.raises(commands.NoPrivateMessage):
            await bot_con_musica.get_command(nombre).can_run(privado)
    vc = FakePlayer()
    afuera = _ctx_de_barra(bot_con_musica, vc, FakeAuthor(channel="otro-canal"))
    adentro = _ctx_de_barra(bot_con_musica, vc, FakeAuthor(channel="canal-de-voz"))
    for nombre in CON_CONTROL:
        comando = bot_con_musica.get_command(nombre)
        with pytest.raises(commands.CheckFailure, match="mismo canal"):
            await comando.can_run(afuera)
        assert await comando.can_run(adentro)
# --- autocompletado de /play y /playnext ---------------------------------------------------------

class FakeResultado:
    def __init__(self, titulo="Never Gonna Give You Up", autor="Rick Astley", largo=225_000,
                 uri="https://www.youtube.com/watch?v=dQw4w9WgXcQ", en_vivo=False):
        self.title, self.author, self.length, self.uri, self.is_stream = titulo, autor, largo, uri, en_vivo

URL = "https://www.youtube.com/watch?v=abc"

@pytest.mark.parametrize("texto", ["", "  ", "a", "ab", "  ab  "])
async def test_con_menos_de_tres_caracteres_no_se_busca_ni_se_sugiere(cog, monkeypatch, texto):
    consultas = _busca(monkeypatch, [FakeResultado()])
    assert await cog.buscar_sugerencias(texto) == [] and consultas == []

@pytest.mark.parametrize("texto, esperado", [
    (f"  {URL} ", [(URL, URL)]),
    ("https://www.youtube.com/playlist?list=" + "a" * 100, []),
], ids=["tal-cual", "no-cabe-en-una-opcion"])
async def test_una_url_se_ofrece_tal_cual_sin_buscar(cog, monkeypatch, texto, esperado):
    consultas = _busca(monkeypatch, [FakeResultado()])
    opciones = await cog.buscar_sugerencias(texto)
    assert [(o.name, o.value) for o in opciones] == esperado and consultas == []

async def test_cada_opcion_es_artista_titulo_y_duracion_con_la_url_como_valor(cog, monkeypatch):
    consultas = _busca(monkeypatch, [FakeResultado(), FakeResultado("Otro", "Alguien", 61_000, "https://youtu.be/x")])
    opciones = await cog.buscar_sugerencias("never gonna")
    assert [(o.name, o.value) for o in opciones] == [
        ("Rick Astley – Never Gonna Give You Up (3:45)", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("Alguien – Otro (1:01)", "https://youtu.be/x"),
    ]
    assert consultas == ["never gonna"]

async def test_un_titulo_enorme_se_recorta_en_el_nombre_y_en_el_valor(cog, monkeypatch):
    _busca(monkeypatch, [FakeResultado(titulo="T" * 300), FakeResultado(titulo="T" * 300, uri=None)])
    largo, sin_url = await cog.buscar_sugerencias("titulo largo")
    assert len(largo.name) == 100
    assert largo.name.startswith("Rick Astley – TTT") and largo.name.endswith("… (3:45)")
    assert sin_url.value == "T" * 100

@pytest.mark.parametrize("uri", [None, "", "https://www.youtube.com/watch?v=" + "a" * 100])
async def test_si_la_url_no_existe_o_no_cabe_el_valor_es_el_titulo(cog, monkeypatch, uri):
    _busca(monkeypatch, [FakeResultado(uri=uri)])
    (opcion,) = await cog.buscar_sugerencias("never gonna")
    assert opcion.value == "Never Gonna Give You Up"

async def test_sin_artista_el_nombre_es_el_titulo_y_un_directo_no_lleva_duracion(cog, monkeypatch):
    _busca(monkeypatch, [FakeResultado("Radio", "", 9_000_000_000, en_vivo=True), FakeResultado("Sin largo", "X", 0)])
    opciones = await cog.buscar_sugerencias("radio")
    assert [o.name for o in opciones] == ["Radio (en vivo)", "X – Sin largo"]

async def test_hay_como_mucho_25_opciones_y_se_saltan_los_temas_sin_titulo(cog, monkeypatch):
    _busca(monkeypatch, [FakeResultado(titulo="")] + [FakeResultado(titulo=f"Tema {i}") for i in range(40)])
    opciones = await cog.buscar_sugerencias("tema")
    assert len(opciones) == 25 and opciones[0].name.startswith("Rick Astley – Tema 0")

async def test_una_playlist_no_se_sugiere(cog, monkeypatch):
    lista = wavelink.Playlist.__new__(wavelink.Playlist)
    lista.name, lista.tracks = "Una lista", [FakeResultado()]
    _busca(monkeypatch, lista)
    assert await cog.buscar_sugerencias("una lista") == []

@pytest.fixture
def reloj_de_sugerencias(monkeypatch):
    marca = {"t": 1000.0}
    monkeypatch.setattr(music_mod, "time", SimpleNamespace(monotonic=lambda: marca["t"]))
    def avanzar(segundos):
        marca["t"] += segundos

    return avanzar

async def test_la_misma_busqueda_sale_de_la_cache_hasta_que_vence(cog, monkeypatch, reloj_de_sugerencias):
    consultas = _busca(monkeypatch, [FakeResultado()])
    primera = await cog.buscar_sugerencias("Never Gonna")
    reloj_de_sugerencias(music_mod.SUGERENCIAS_VIGENCIA - 1)
    otra = await cog.buscar_sugerencias("  never gonna ")
    assert consultas == ["Never Gonna"] and otra == primera
    reloj_de_sugerencias(2)
    await cog.buscar_sugerencias("never gonna")
    assert consultas == ["Never Gonna", "never gonna"]

async def test_una_busqueda_sin_resultados_no_se_guarda(cog, monkeypatch, reloj_de_sugerencias):
    consultas = _busca(monkeypatch, [])
    await cog.buscar_sugerencias("nada de nada")
    await cog.buscar_sugerencias("nada de nada")
    assert len(consultas) == 2 and cog._sugerencias == {}

async def test_la_cache_tiene_tope_y_suelta_primero_lo_mas_viejo(cog, monkeypatch, reloj_de_sugerencias):
    monkeypatch.setattr(music_mod, "SUGERENCIAS_EN_CACHE", 2)
    consultas = _busca(monkeypatch, [FakeResultado()])
    for texto in ("uno", "dos", "tres"):
        await cog.buscar_sugerencias(texto)
    assert list(cog._sugerencias) == ["dos", "tres"]
    await cog.buscar_sugerencias("uno")
    assert consultas == ["uno", "dos", "tres", "uno"]

async def test_lo_vencido_se_limpia_al_guardar_algo_nuevo(cog, monkeypatch, reloj_de_sugerencias):
    _busca(monkeypatch, [FakeResultado()])
    await cog.buscar_sugerencias("viejo")
    reloj_de_sugerencias(music_mod.SUGERENCIAS_VIGENCIA + 1)
    await cog.buscar_sugerencias("nuevo")
    assert list(cog._sugerencias) == ["nuevo"]

async def test_si_lavalink_tarda_mas_del_plazo_no_hay_opciones(cog, monkeypatch):
    async def _lenta(query, *a, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(music_mod.wavelink.Playable, "search", _lenta)
    monkeypatch.setattr(music_mod, "SUGERENCIAS_ESPERA", 0.01)
    assert await asyncio.wait_for(cog.buscar_sugerencias("tema lento"), 1) == [] and cog._sugerencias == {}

@pytest.mark.parametrize("error", [wavelink.InvalidNodeException("sin nodo"), RuntimeError("se cayó")])
async def test_si_la_busqueda_falla_no_hay_opciones_y_nada_se_rompe(cog, monkeypatch, error):
    _busca(monkeypatch, error)
    assert await cog.buscar_sugerencias("tema roto") == []

async def test_el_autocompletado_de_barra_busca_en_un_servidor_permitido(cog, monkeypatch):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({7}))
    _busca(monkeypatch, [FakeResultado()])
    opciones = await cog.autocompletar_busqueda(SimpleNamespace(guild_id=7), "never gonna")
    assert [o.name for o in opciones] == ["Rick Astley – Never Gonna Give You Up (3:45)"]

@pytest.mark.parametrize("permitidos, guild_id", [(frozenset({7}), 8), (frozenset(), None), (frozenset({7}), None)])
async def test_el_autocompletado_no_busca_para_un_servidor_ajeno_ni_por_privado(cog, monkeypatch, permitidos, guild_id):
    """Discord no pasa el autocompletado por los checks: el de servidores tiene que repetirse aquí."""
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", permitidos)
    consultas = _busca(monkeypatch, [FakeResultado()])
    assert await cog.autocompletar_busqueda(SimpleNamespace(guild_id=guild_id), "never gonna") == [] and consultas == []
# --- la letra en vivo ------------------------------------------------------------------------------

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
    vc.guild.voice_client, vc.current, vc.pos = vc, FakeTrack(titulo), 0
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
    await _inicio(cog, vc, vc.current)

async def _publicada(vc, cantidad=1):
    await _esperar(lambda: len(vc.text_channel.mensajes) >= cantidad)

@pytest.mark.parametrize("modo, esperado", [("", [True, False, True]), ("on", [True, True, True]), ("off", [False, False, False])])
async def test_letra_alterna_o_fija_el_estado_y_responde_en_privado(cog, en_vivo, modo, esperado):
    ctx = FakeCtx(_sonando(en_vivo))
    estados = []
    for _ in esperado:
        await cog.letra_cmd(cog, ctx, modo)
        estados.append(cog.estacion(1).letra.activa)
    assert estados == esperado and ctx.opciones == [{"ephemeral": True}] * 3

async def test_letra_encendida_apagada_y_encendida_otra_vez(cog, en_vivo):
    vc = _sonando(en_vivo)
    ctx = FakeCtx(vc)
    letra = cog.estacion(1).letra
    await cog.letra_cmd(cog, ctx, "on")
    await _publicada(vc)
    assert "encendida" in ctx.ultimo and letra.tarea is not None
    assert vc.text_channel.mensajes[0].contenido == armar_texto("Tema A", LETRA_EN_VIVO.lineas, 0)
    await cog.letra_cmd(cog, ctx, "on")
    await _publicada(vc, 2)
    assert [m.borrado for m in vc.text_channel.mensajes] == [True, False]    # vuelve a salir al final del chat
    tarea = letra.tarea
    await cog.letra_cmd(cog, ctx, "off")
    assert "apagada" in ctx.ultimo and not letra.activa and letra.mensaje is None and vc.text_channel.vivos == []
    await asyncio.gather(tarea, return_exceptions=True)
    assert tarea.cancelled()

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
    assert not cog.estacion(1).letra.activa and cog.estacion(1).letra.tarea is None and vc.text_channel.mensajes == []

@pytest.mark.parametrize("modo, activa", [("ON", True), ("Off", False)])
async def test_letra_no_distingue_mayusculas(cog, en_vivo, modo, activa):
    cog.estacion(1).letra.activa = not activa
    await cog.letra_cmd(cog, FakeCtx(_sonando(en_vivo)), modo)
    assert cog.estacion(1).letra.activa is activa

@pytest.mark.parametrize("modo, con_canal, aviso", [("quizas", True, "Uso:"), ("on", False, "canal de texto")])
async def test_letra_con_un_argumento_raro_o_sin_canal_de_texto_avisa_y_no_cambia_nada(cog, en_vivo, modo, con_canal, aviso):
    ctx = FakeCtx(_sonando(en_vivo, con_canal=con_canal))
    await cog.letra_cmd(cog, ctx, modo)
    assert aviso in ctx.ultimo and not cog.estacion(1).letra.activa

async def test_letra_encendida_mientras_habla_el_dj_solo_avisa_que_habla(cog, en_vivo):
    vc = _sonando(en_vivo)
    vc.current = _clip_del_dj(f"{music_mod.tts.CACHE_DIR}/dj_abc.wav")
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
    await _inicio(cog, vc, _clip_del_dj())
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
    await _fin(cog, vc, vc.current)
    letra = cog.estacion(1).letra
    assert not letra.activa and letra.mensaje is None and vc.text_channel.vivos == []
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
    await _fin(cog, vc, vc.current, motivo)
    assert cog.estacion(1).letra.activa and len(vc.text_channel.vivos) == 1

def test_la_cache_de_letras_es_una_por_cog():
    assert Music(bot=None).letras is not Music(bot=None).letras
# --- el panel de botones y el cog -----------------------------------------------------------------

def _interaccion_de_panel(vc):
    yo = SimpleNamespace(guild=vc.guild, editados=[], privados=[], data={})
    async def edit_message(**kwargs):
        yo.editados.append(kwargs)
    async def send_message(contenido, **kwargs):
        yo.privados.append(contenido)

    yo.response = SimpleNamespace(edit_message=edit_message, send_message=send_message)
    return yo

def _player_de_panel():
    vc = FakePlayer(playing=True)
    vc.guild.voice_client, vc.current = vc, FakeTrack("Tema A")
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
    vista = PlayerPanel(cog)
    yo = _interaccion_de_panel(vc)
    await vista.letra_en_vivo.callback(yo)
    assert cog.estacion(1).letra.activa and vista.letra_en_vivo.style is discord.ButtonStyle.success
    assert len(yo.editados) == 1 and yo.privados == []
    await vista.letra_en_vivo.callback(_interaccion_de_panel(vc))
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

async def test_al_cargar_el_cog_registra_la_vista_persistente_del_panel(cog, lifecycle):
    class BotQueRegistraVistas:
        """Valida como `Client.add_view`: solo acepta vistas persistentes."""
        def __init__(self):
            self.vistas = []
        def add_view(self, vista):
            if not vista.is_persistent():
                raise ValueError("View is not persistent")
            self.vistas.append(vista)

    cog.bot = BotQueRegistraVistas()
    await cog.cog_load()
    assert cog.bot.vistas == [cog._vista] and isinstance(cog._vista, PlayerPanel)
    await cog.cog_unload()
    assert cog._vista.is_finished()
# --- al apagar el bot -----------------------------------------------------------------------------

CIERRES = [
    pytest.param(lambda cog: cog.guardar_y_cerrar(), id="guardar_y_cerrar"),
    pytest.param(lambda cog: cog.cog_unload(), id="cog_unload"),
]

async def _con_panel_y_letra(cog, en_vivo, guild_id):
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
    assert bueno.panel is None and "No pude borrar los mensajes de 1" in caplog.text

async def test_guardar_y_cerrar_borra_los_mensajes_aunque_no_se_haya_retomado_nada(cog):
    est = cog.estacion(1)
    est.panel = (MensajeDePanel(), VistaDePanel())
    mensaje = est.panel[0]
    await cog.guardar_y_cerrar()
    assert cog._persistencia == "cerrada" and mensaje.borrado
