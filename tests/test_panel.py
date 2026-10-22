"""El panel de botones: que no pise métodos de discord.ui.View, que muestre su estado y qué responde.

Un handler llamado `stop` reemplaza a View.stop() por un objeto Button, y
View.stop() se llama al borrar el panel y al descargar el cog. Resultado:
'Button' object is not callable, y el botón ⏹️ reventaba en el primer clic.
"""

from collections import Counter
from types import SimpleNamespace

import discord
import pytest
import wavelink

from core.panel import ID_COLA, PlayerPanel

GUILD = SimpleNamespace(id=1)


class FakeDJ:
    def __init__(self):
        self.disponible, self.forzados = True, []

    def available(self):
        return self.disponible

    def force(self, guild_id):
        self.forzados.append(guild_id)


class FakeCog:
    def __init__(self):
        self.letra_pedida, self.autoplay_pedido, self.autoplay = [], [], False
        self.est = SimpleNamespace(letra=SimpleNamespace(activa=False))
        self.dj = FakeDJ()

    def estacion(self, guild_id):
        return self.est

    def autoplay_activo(self, guild_id):
        return self.autoplay

    def poner_autoplay(self, guild_id, vc, activo):
        self.autoplay_pedido.append((guild_id, vc, activo))
        self.autoplay = activo
        vc.autoplay = wavelink.AutoPlayMode.enabled if activo else wavelink.AutoPlayMode.partial
        return "autoplay encendido" if activo else "autoplay apagado"

    async def alternar_letra(self, guild, modo=None):
        self.letra_pedida.append((guild, modo))
        self.est.letra.activa = not self.est.letra.activa
        return "letra prendida" if self.est.letra.activa else "letra apagada"


class FakeQueue(list):
    """La cola de wavelink, con su `mode` y un `shuffle` que da vuelta el orden para que se note."""

    def __init__(self, titulos=()):
        super().__init__(SimpleNamespace(title=t, length=60_000) for t in titulos)
        self.mode = wavelink.QueueMode.normal

    def shuffle(self):
        self.reverse()


class FakePlayer:
    def __init__(self, titulos=()):
        self.channel = "voz"
        self.paused, self.playing, self.volume, self.saltados = False, True, 50, 0
        self.current = SimpleNamespace(title="Tema", length=180_000)
        self.queue, self.auto_queue = FakeQueue(titulos), []
        self.autoplay = wavelink.AutoPlayMode.partial

    async def pause(self, valor):
        self.paused = valor

    async def set_volume(self, pct):
        self.volume = pct

    async def skip(self, force=False):
        self.saltados += 1


class FakeRespuesta:
    def __init__(self):
        self.enviados, self.editados = [], []

    async def send_message(self, contenido, **kwargs):
        self.enviados.append((contenido, kwargs))

    async def edit_message(self, **kwargs):
        self.editados.append(kwargs)


class FakeInteraccion:
    """Quien toca el panel: por defecto está en el mismo canal de voz que el bot."""

    def __init__(self, voz_del_usuario="voz", boton=None, vc=None, guild_id=1):
        self.vc = vc or FakePlayer()
        self.guild = SimpleNamespace(id=guild_id, voice_client=self.vc)
        self.user = SimpleNamespace(voice=SimpleNamespace(channel=voz_del_usuario) if voz_del_usuario else None)
        self.data = {"custom_id": boton.custom_id} if boton else {}
        self.response = FakeRespuesta()


@pytest.fixture
def view():
    return PlayerPanel(FakeCog())


async def _pulsa(boton, **kwargs):
    yo = FakeInteraccion(boton=boton, **kwargs)
    await boton.callback(yo)
    return yo


def test_view_stop_sigue_siendo_llamable(view):
    assert callable(view.stop), "un handler llamado 'stop' pisó View.stop()"
    assert PlayerPanel.stop is discord.ui.View.stop
    view.stop()   # esto es lo que hace _delete_panel; con el bug tiraba TypeError


def test_nada_de_la_clase_pisa_la_api_de_view():
    """Cualquier atributo de PlayerPanel que se llame igual que uno de View lo
    reemplaza: handlers de botón, helpers, properties, todo.

    Incluye la API privada a propósito. discord.py llama internamente a cosas
    como View._refresh(components) al actualizarse el mensaje; pisarla con un
    helper propio tira el bot entero desde el parseo del gateway.
    """
    # Estos SÍ están pensados para sobreescribirse.
    hooks = {"interaction_check", "on_timeout", "on_error"}
    reservados = {
        name for name in dir(discord.ui.View)
        if not name.startswith("__")
        and name not in hooks
    }
    pisados = sorted(set(vars(PlayerPanel)) & reservados)
    assert not pisados, f"pisan la API de View: {pisados}"


def test_estan_los_once_botones_en_tres_filas(view):
    assert [(b.emoji.name, b.row) for b in view.children] == [
        ("⏸️", 0), ("⏭️", 0), ("⏹️", 0), ("🔁", 0), ("🔀", 0),
        ("🔉", 1), ("🔊", 1), ("📋", 1),
        ("🎙️", 2), ("🎤", 2), ("🔮", 2),
    ]


def test_el_boton_de_la_letra_no_se_confunde_con_el_del_dj(view):
    por_emoji = {b.emoji.name: b for b in view.children}

    assert por_emoji["🎤"].label == "Letra" and por_emoji["🎙️"].label is None


def test_ninguna_fila_pasa_de_cinco_botones(view):
    assert max(Counter(b.row for b in view.children).values()) <= 5


def test_todos_los_botones_tienen_custom_id_fijo_y_unico(view):
    ids = [b.custom_id for b in view.children]

    assert len(ids) == 11 and all(ids) and len(set(ids)) == len(ids)
    assert [b.custom_id for b in PlayerPanel(FakeCog()).children] == ids    # fijo: no cambia de una vista a otra
    assert ID_COLA in ids
    assert view.is_persistent()


async def test_una_sola_vista_atiende_a_cualquier_servidor(view):
    await _pulsa(view.dj, guild_id=1)
    await _pulsa(view.dj, guild_id=2)

    assert view.cog.dj.forzados == [1, 2]


# --- el estado de los botones ---------------------------------------------------------------

@pytest.mark.parametrize("pausado, emoji", [(False, "⏸️"), (True, "▶️")])
def test_el_boton_de_pausa_muestra_lo_que_hara(view, pausado, emoji):
    vc = FakePlayer()
    vc.paused = pausado

    embed = view.pintar(vc, GUILD)

    assert view.toggle_pause.emoji.name == emoji
    assert embed.title.startswith("⏸️") is pausado      # y el embed sale del mismo player


@pytest.mark.parametrize("modo, estilo", [
    (wavelink.QueueMode.normal, discord.ButtonStyle.secondary),
    (wavelink.QueueMode.loop, discord.ButtonStyle.success),
])
def test_el_boton_de_loop_va_en_verde_con_el_loop_activo(view, modo, estilo):
    vc = FakePlayer()
    vc.queue.mode = modo

    view.pintar(vc, GUILD)

    assert view.loop.style is estilo


@pytest.mark.parametrize("activo, estilo", [(False, discord.ButtonStyle.secondary), (True, discord.ButtonStyle.success)])
def test_el_boton_de_autoplay_va_en_verde_con_el_autoplay_activo(view, activo, estilo):
    view.cog.autoplay = activo

    view.pintar(FakePlayer(), GUILD)

    assert view.autoplay.style is estilo


@pytest.mark.parametrize("activa, estilo", [(False, discord.ButtonStyle.secondary), (True, discord.ButtonStyle.success)])
def test_el_boton_de_la_letra_va_en_verde_con_la_letra_activa(view, activa, estilo):
    view.cog.est.letra.activa = activa

    view.pintar(FakePlayer(), GUILD)

    assert view.letra_en_vivo.style is estilo


def test_pintar_vuelve_a_dejar_cada_boton_como_estaba_al_cambiar_el_estado(view):
    """Con la vista compartida entre servidores, un estado no se puede quedar pegado en el siguiente."""
    vc = FakePlayer()
    vc.paused, vc.queue.mode, view.cog.autoplay, view.cog.est.letra.activa = True, wavelink.QueueMode.loop, True, True
    view.pintar(vc, GUILD)
    vc.paused, vc.queue.mode, view.cog.autoplay, view.cog.est.letra.activa = False, wavelink.QueueMode.normal, False, False

    view.pintar(vc, GUILD)

    assert view.toggle_pause.emoji.name == "⏸️"
    assert {view.loop.style, view.autoplay.style, view.letra_en_vivo.style} == {discord.ButtonStyle.secondary}


def test_sin_player_el_panel_se_pinta_igual(view):
    assert view.pintar(None, GUILD).title.endswith("Nada sonando")
    assert view.toggle_pause.emoji.name == "⏸️"


# --- qué responde cada botón ----------------------------------------------------------------

@pytest.mark.parametrize("nombre", ["toggle_pause", "loop", "vol_down", "vol_up", "autoplay", "letra_en_vivo"])
async def test_lo_que_se_ve_en_el_panel_se_edita_sin_aviso_aparte(view, nombre):
    yo = await _pulsa(getattr(view, nombre))

    assert yo.response.enviados == []
    assert len(yo.response.editados) == 1
    editado = yo.response.editados[0]
    assert editado["view"] is view and isinstance(editado["embed"], discord.Embed)


async def test_pausar_repinta_el_boton_y_el_titulo(view):
    yo = await _pulsa(view.toggle_pause)

    assert yo.vc.paused and view.toggle_pause.emoji.name == "▶️"
    assert yo.response.editados[0]["embed"].title.startswith("⏸️")


async def test_el_volumen_repinta_el_embed_con_el_valor_nuevo(view):
    yo = await _pulsa(view.vol_down)

    volumen = next(f.value for f in yo.response.editados[0]["embed"].fields if f.name == "Volumen")
    assert volumen == "40%" and yo.vc.volume == 40


async def test_el_boton_de_loop_activa_y_desactiva_el_loop(view):
    vc = FakePlayer()

    await _pulsa(view.loop, vc=vc)
    assert vc.queue.mode is wavelink.QueueMode.loop and view.loop.style is discord.ButtonStyle.success
    await _pulsa(view.loop, vc=vc)
    assert vc.queue.mode is wavelink.QueueMode.normal and view.loop.style is discord.ButtonStyle.secondary


async def test_el_boton_de_autoplay_alterna_el_autoplay_del_servidor(view):
    vc = FakePlayer()

    await _pulsa(view.autoplay, vc=vc)
    assert view.cog.autoplay and view.autoplay.style is discord.ButtonStyle.success
    await _pulsa(view.autoplay, vc=vc)
    assert not view.cog.autoplay and view.autoplay.style is discord.ButtonStyle.secondary

    assert view.cog.autoplay_pedido == [(1, vc, True), (1, vc, False)]


async def test_el_boton_de_la_letra_alterna_y_repinta_sin_aviso_aparte(view):
    yo = await _pulsa(view.letra_en_vivo)

    assert view.cog.letra_pedida == [(yo.guild, None)]      # sin modo: alterna
    assert view.cog.est.letra.activa and view.letra_en_vivo.style is discord.ButtonStyle.success
    assert yo.response.enviados == [] and len(yo.response.editados) == 1

    await _pulsa(view.letra_en_vivo)
    assert not view.cog.est.letra.activa and view.letra_en_vivo.style is discord.ButtonStyle.secondary


async def test_mezclar_edita_el_panel_si_cambia_el_despues(view):
    yo = await _pulsa(view.shuffle, vc=FakePlayer(["a", "b", "c"]))       # queda "c" primero

    despues = next(f.value for f in yo.response.editados[0]["embed"].fields if f.name == "Después")
    assert despues == "c" and yo.response.enviados == []


@pytest.mark.parametrize("titulos, aviso", [(["a", "b", "a"], "Cola mezclada 🔀"), ([], "La cola está vacía.")])
async def test_mezclar_sin_cambio_visible_avisa_aparte(view, titulos, aviso):
    yo = await _pulsa(view.shuffle, vc=FakePlayer(titulos))

    assert yo.response.enviados == [(aviso, {"ephemeral": True})] and yo.response.editados == []


async def test_un_error_se_avisa_en_privado_y_no_toca_el_panel(view):
    vc = FakePlayer()
    vc.playing = False

    yo = await _pulsa(view.toggle_pause, vc=vc)

    assert yo.response.enviados == [("No hay nada reproduciendo.", {"ephemeral": True})] and yo.response.editados == []


async def test_saltar_responde_en_privado_y_no_edita_el_panel(view):
    yo = await _pulsa(view.skip)

    assert yo.response.enviados == [("Skipped ⏭️", {"ephemeral": True})]
    assert yo.response.editados == [] and yo.vc.saltados == 1


async def test_ver_la_cola_responde_en_privado_y_no_edita_el_panel(view):
    yo = await _pulsa(view.show_queue, vc=FakePlayer(["uno", "dos"]))

    contenido, kwargs = yo.response.enviados[0]
    assert kwargs == {"ephemeral": True} and "Ahora" in contenido and "uno" in contenido and "dos" in contenido
    assert yo.response.editados == []


async def test_el_dj_responde_en_privado_y_habla_en_el_proximo_corte(view):
    yo = await _pulsa(view.dj)

    assert view.cog.dj.forzados == [1]
    assert yo.response.enviados == [("🎙️ El DJ habla en el próximo corte.", {"ephemeral": True})] and yo.response.editados == []


async def test_el_dj_apagado_lo_avisa_en_privado(view):
    view.cog.dj.disponible = False

    yo = await _pulsa(view.dj)

    assert view.cog.dj.forzados == [] and "apagado" in yo.response.enviados[0][0] and yo.response.editados == []


# --- quién puede tocar ----------------------------------------------------------------------

async def test_los_botones_piden_estar_en_el_mismo_canal_de_voz_que_el_bot(view):
    for boton in view.children:
        assert await view.interaction_check(FakeInteraccion(boton=boton)) is True

    assert view.cog.letra_pedida == []


@pytest.mark.parametrize("voz", ["otro", None])
async def test_quien_no_esta_en_el_canal_solo_puede_ver_la_cola(view, voz):
    for boton in view.children:
        yo = FakeInteraccion(voz_del_usuario=voz, boton=boton)
        permitido = await view.interaction_check(yo)
        assert permitido is (boton is view.show_queue), boton.custom_id
        if not permitido:
            assert "mismo canal de voz" in yo.response.enviados[0][0]
    assert view.cog.letra_pedida == []


async def test_ver_la_cola_sin_estar_en_el_canal_la_muestra(view):
    yo = FakeInteraccion(voz_del_usuario=None, boton=view.show_queue, vc=FakePlayer(["uno"]))

    assert await view.interaction_check(yo) is True
    await view.show_queue.callback(yo)

    assert "Ahora" in yo.response.enviados[0][0] and "uno" in yo.response.enviados[0][0]
