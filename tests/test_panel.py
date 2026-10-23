"""El panel de botones: que no pise métodos de discord.ui.View, que muestre su estado y qué responde.

Un handler llamado `stop` reemplazaba View.stop() por un Button, y View.stop() se llama al
borrar el panel y al descargar el cog: 'Button' object is not callable en el primer clic de ⏹️.
"""

from types import SimpleNamespace

import discord
import pytest
import wavelink

from core.panel import ID_COLA, PlayerPanel

GUILD = SimpleNamespace(id=1)
VERDE, GRIS = discord.ButtonStyle.success, discord.ButtonStyle.secondary
LOOP, NORMAL = wavelink.QueueMode.loop, wavelink.QueueMode.normal


class FakeDJ:
    def __init__(self):
        self.disponible, self.forzados = True, []

    def available(self):
        return self.disponible

    def force(self, guild_id):
        self.forzados.append(guild_id)


class FakeCog:
    def __init__(self):
        self.letra_pedida, self.autoplay_pedido, self.radio_pedida, self.autoplay = [], [], [], False
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

    async def poner_radio(self, guild):
        self.radio_pedida.append(guild)
        if not self.autoplay:
            self.poner_autoplay(guild.id, guild.voice_client, True)
        return "📻 Radio armada a partir de **Tema** (3 temas)"

    async def alternar_letra(self, guild, modo=None):
        self.letra_pedida.append((guild, modo))
        self.est.letra.activa = not self.est.letra.activa
        return "letra prendida" if self.est.letra.activa else "letra apagada"


class FakeQueue(list):
    """La cola de wavelink, con su `mode` y un `shuffle` que da vuelta el orden para que se note."""

    def __init__(self, titulos=()):
        super().__init__(SimpleNamespace(title=t, length=60_000) for t in titulos)
        self.mode = NORMAL

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
        self.enviados, self.editados, self.diferidas = [], [], []

    async def defer(self, **kwargs):
        self.diferidas.append(kwargs)

    async def send_message(self, contenido, **kwargs):
        self.enviados.append((contenido, kwargs))

    async def edit_message(self, **kwargs):
        self.editados.append(kwargs)


class FakeSeguimiento:
    """`interaction.followup`: lo que se manda después de diferir la respuesta."""

    def __init__(self):
        self.enviados = []

    async def send(self, contenido, **kwargs):
        self.enviados.append((contenido, kwargs))


class FakeMensaje:
    """El mensaje del panel: `interaction.message`."""

    def __init__(self):
        self.editados = []

    async def edit(self, **kwargs):
        self.editados.append(kwargs)


class FakeInteraccion:
    """Quien toca el panel: por defecto está en el mismo canal de voz que el bot."""

    def __init__(self, voz_del_usuario="voz", boton=None, vc=None, guild_id=1):
        self.vc = vc or FakePlayer()
        self.guild = SimpleNamespace(id=guild_id, voice_client=self.vc)
        self.user = SimpleNamespace(voice=SimpleNamespace(channel=voz_del_usuario) if voz_del_usuario else None)
        self.data = {"custom_id": boton.custom_id} if boton else {}
        self.response, self.followup, self.message = FakeRespuesta(), FakeSeguimiento(), FakeMensaje()


@pytest.fixture
def view():
    return PlayerPanel(FakeCog())


async def _pulsa(boton, **kwargs):
    yo = FakeInteraccion(boton=boton, **kwargs)
    await boton.callback(yo)
    return yo


def _estado(view, vc, *, pausado=False, loop=False, autoplay=False, letra=False):
    vc.paused, vc.queue.mode = pausado, LOOP if loop else NORMAL
    view.cog.autoplay, view.cog.est.letra.activa = autoplay, letra


def _campo(yo, nombre):
    return next(f.value for f in yo.response.editados[0]["embed"].fields if f.name == nombre)


def _privado(yo, aviso):
    assert yo.response.enviados == [(aviso, {"ephemeral": True})] and yo.response.editados == []


def test_view_stop_sigue_siendo_llamable(view):
    assert callable(view.stop), "un handler llamado 'stop' pisó View.stop()"
    assert PlayerPanel.stop is discord.ui.View.stop
    view.stop()   # esto es lo que hace _delete_panel; con el bug tiraba TypeError


def test_nada_de_la_clase_pisa_la_api_de_view():
    """Cualquier atributo de PlayerPanel con el nombre de uno de View lo reemplaza, también la API privada:
    discord.py llama a View._refresh(components) al actualizarse un mensaje y pisarla tumbó el bot entero."""
    hooks = {"interaction_check", "on_timeout", "on_error"}     # estos SÍ se sobreescriben
    reservados = {n for n in dir(discord.ui.View) if not n.startswith("__") and n not in hooks}
    pisados = sorted(set(vars(PlayerPanel)) & reservados)
    assert not pisados, f"pisan la API de View: {pisados}"


def test_estan_los_doce_botones_en_tres_filas_y_la_letra_no_se_confunde_con_el_dj(view):
    assert [(b.emoji.name, b.row) for b in view.children] == [
        ("⏸️", 0), ("⏭️", 0), ("⏹️", 0), ("🔁", 0), ("🔀", 0),
        ("🔉", 1), ("🔊", 1), ("📋", 1),
        ("🎙️", 2), ("🎤", 2), ("🔮", 2), ("📻", 2),
    ]
    por_emoji = {b.emoji.name: b for b in view.children}
    assert por_emoji["🎤"].label == "Letra" and por_emoji["🎙️"].label is None and por_emoji["📻"].label == "Radio"


def test_todos_los_botones_tienen_custom_id_fijo_y_unico(view):
    ids = [b.custom_id for b in view.children]

    assert len(ids) == 12 and all(ids) and len(set(ids)) == len(ids) and "lavadj:radio" in ids
    assert [b.custom_id for b in PlayerPanel(FakeCog()).children] == ids    # fijo: no cambia de una vista a otra
    assert ID_COLA in ids
    assert view.is_persistent()


async def test_una_sola_vista_atiende_a_cualquier_servidor(view):
    await _pulsa(view.dj, guild_id=1)
    await _pulsa(view.dj, guild_id=2)
    assert view.cog.dj.forzados == [1, 2]


@pytest.mark.parametrize("pausado, emoji", [(False, "⏸️"), (True, "▶️")])
def test_el_boton_de_pausa_muestra_lo_que_hara(view, pausado, emoji):
    vc = FakePlayer()
    vc.paused = pausado

    embed = view.pintar(vc, GUILD)

    assert view.toggle_pause.emoji.name == emoji
    assert embed.title.startswith("⏸️") is pausado      # y el embed sale del mismo player


@pytest.mark.parametrize("estado, boton", [("loop", "loop"), ("autoplay", "autoplay"), ("letra", "letra_en_vivo")])
@pytest.mark.parametrize("activo", [False, True])
def test_los_botones_de_estado_van_en_verde_con_su_estado_activo(view, estado, boton, activo):
    vc = FakePlayer()
    _estado(view, vc, **{estado: activo})

    view.pintar(vc, GUILD)

    assert getattr(view, boton).style is (VERDE if activo else GRIS)


def test_pintar_vuelve_a_dejar_cada_boton_como_estaba_al_cambiar_el_estado(view):
    """Con la vista compartida entre servidores, un estado no se puede quedar pegado en el siguiente."""
    vc = FakePlayer()
    _estado(view, vc, pausado=True, loop=True, autoplay=True, letra=True)
    view.pintar(vc, GUILD)
    _estado(view, vc)

    view.pintar(vc, GUILD)

    assert view.toggle_pause.emoji.name == "⏸️"
    assert {view.loop.style, view.autoplay.style, view.letra_en_vivo.style} == {GRIS}


def test_sin_player_el_panel_se_pinta_igual(view):
    assert view.pintar(None, GUILD).title.endswith("Nada sonando")
    assert view.toggle_pause.emoji.name == "⏸️"


@pytest.mark.parametrize("nombre", ["toggle_pause", "loop", "vol_down", "vol_up", "autoplay", "letra_en_vivo"])
async def test_lo_que_se_ve_en_el_panel_se_edita_sin_aviso_aparte(view, nombre):
    yo = await _pulsa(getattr(view, nombre))

    assert yo.response.enviados == [] and len(yo.response.editados) == 1
    editado = yo.response.editados[0]
    assert editado["view"] is view and isinstance(editado["embed"], discord.Embed)


async def test_pausar_repinta_el_boton_y_el_titulo(view):
    yo = await _pulsa(view.toggle_pause)
    assert yo.vc.paused and view.toggle_pause.emoji.name == "▶️"
    assert yo.response.editados[0]["embed"].title.startswith("⏸️")


async def test_el_volumen_repinta_el_embed_con_el_valor_nuevo(view):
    yo = await _pulsa(view.vol_down)
    assert _campo(yo, "Volumen") == "40%" and yo.vc.volume == 40


async def test_el_boton_de_loop_activa_y_desactiva_el_loop(view):
    vc = FakePlayer()
    await _pulsa(view.loop, vc=vc)
    assert vc.queue.mode is LOOP and view.loop.style is VERDE
    await _pulsa(view.loop, vc=vc)
    assert vc.queue.mode is NORMAL and view.loop.style is GRIS


async def test_el_boton_de_autoplay_alterna_el_autoplay_del_servidor(view):
    vc = FakePlayer()
    await _pulsa(view.autoplay, vc=vc)
    assert view.cog.autoplay and view.autoplay.style is VERDE
    await _pulsa(view.autoplay, vc=vc)
    assert not view.cog.autoplay and view.autoplay.style is GRIS
    assert view.cog.autoplay_pedido == [(1, vc, True), (1, vc, False)]


async def test_el_boton_de_la_letra_alterna_y_repinta_sin_aviso_aparte(view):
    yo = await _pulsa(view.letra_en_vivo)

    assert view.cog.letra_pedida == [(yo.guild, None)]      # sin modo: alterna
    assert view.cog.est.letra.activa and view.letra_en_vivo.style is VERDE
    assert yo.response.enviados == [] and len(yo.response.editados) == 1

    await _pulsa(view.letra_en_vivo)
    assert not view.cog.est.letra.activa and view.letra_en_vivo.style is GRIS


async def test_el_boton_de_radio_rearma_y_responde_en_privado_con_el_aviso_del_cog(view):
    yo = await _pulsa(view.radio)

    assert view.cog.radio_pedida == [yo.guild]
    assert yo.response.diferidas == [{"ephemeral": True, "thinking": True}]     # buscar el mix tarda más que los 3 s de Discord
    assert yo.followup.enviados == [("📻 Radio armada a partir de **Tema** (3 temas)", {"ephemeral": True})]
    assert yo.response.enviados == [] and yo.response.editados == []


async def test_el_boton_de_radio_enciende_el_autoplay_y_repinta_el_panel(view):
    vc = FakePlayer()
    _estado(view, vc, autoplay=False)

    yo = await _pulsa(view.radio, vc=vc)

    assert view.cog.autoplay_pedido == [(1, vc, True)] and view.autoplay.style is VERDE
    assert len(yo.message.editados) == 1 and yo.message.editados[0]["view"] is view
    assert "🔮 autoplay" in yo.message.editados[0]["embed"].description


async def test_el_boton_de_radio_no_toca_el_panel_si_no_cambia_nada(view):
    vc = FakePlayer()
    _estado(view, vc, autoplay=True)

    yo = await _pulsa(view.radio, vc=vc)

    assert view.cog.autoplay_pedido == [] and yo.message.editados == [] and len(yo.followup.enviados) == 1


async def test_mezclar_edita_el_panel_si_cambia_el_despues(view):
    yo = await _pulsa(view.shuffle, vc=FakePlayer(["a", "b", "c"]))      # queda "c" primero
    assert _campo(yo, "Después") == "c" and yo.response.enviados == []


@pytest.mark.parametrize("titulos, aviso", [(["a", "b", "a"], "Cola mezclada 🔀"), ([], "La cola está vacía.")])
async def test_mezclar_sin_cambio_visible_avisa_aparte(view, titulos, aviso):
    _privado(await _pulsa(view.shuffle, vc=FakePlayer(titulos)), aviso)


async def test_un_error_se_avisa_en_privado_y_no_toca_el_panel(view):
    vc = FakePlayer()
    vc.playing = False
    _privado(await _pulsa(view.toggle_pause, vc=vc), "No hay nada reproduciendo.")


async def test_saltar_responde_en_privado_y_no_edita_el_panel(view):
    yo = await _pulsa(view.skip)
    _privado(yo, "Skipped ⏭️")
    assert yo.vc.saltados == 1


async def test_el_dj_responde_en_privado_y_habla_en_el_proximo_corte(view):
    yo = await _pulsa(view.dj)
    _privado(yo, "🎙️ El DJ habla en el próximo corte.")
    assert view.cog.dj.forzados == [1]


async def test_el_dj_apagado_lo_avisa_en_privado(view):
    view.cog.dj.disponible = False

    yo = await _pulsa(view.dj)

    assert view.cog.dj.forzados == [] and "apagado" in yo.response.enviados[0][0] and yo.response.editados == []


async def test_ver_la_cola_sin_estar_en_el_canal_la_muestra_en_privado_sin_editar_el_panel(view):
    yo = FakeInteraccion(voz_del_usuario=None, boton=view.show_queue, vc=FakePlayer(["uno", "dos"]))

    assert await view.interaction_check(yo) is True
    await view.show_queue.callback(yo)

    contenido, kwargs = yo.response.enviados[0]
    assert kwargs == {"ephemeral": True} and all(t in contenido for t in ("Ahora", "uno", "dos"))
    assert yo.response.editados == []


@pytest.mark.parametrize("voz", ["voz", "otro", None], ids=["mismo_canal", "otro_canal", "sin_canal"])
async def test_solo_quien_esta_en_el_canal_del_bot_toca_los_botones_y_la_cola_la_ve_cualquiera(view, voz):
    for boton in view.children:
        yo = FakeInteraccion(voz_del_usuario=voz, boton=boton)
        permitido = await view.interaction_check(yo)
        assert permitido is (voz == "voz" or boton is view.show_queue), boton.custom_id
        if not permitido:
            assert "mismo canal de voz" in yo.response.enviados[0][0]
    assert view.cog.letra_pedida == [] and view.cog.radio_pedida == []
