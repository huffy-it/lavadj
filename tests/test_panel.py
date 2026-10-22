"""El panel no debe pisar métodos de discord.ui.View.

Un handler llamado `stop` reemplaza a View.stop() por un objeto Button, y
View.stop() se llama al borrar el panel y al descargar el cog. Resultado:
'Button' object is not callable, y el botón ⏹️ reventaba en el primer clic.
"""

from collections import Counter
from types import SimpleNamespace

import discord
import pytest

from core.panel import PlayerPanel


class FakeCog:
    def __init__(self):
        self.letra_pedida, self.letra_prendida = [], False

    def get_queue(self, guild_id):
        return None

    async def alternar_letra(self, guild, modo=None):
        self.letra_pedida.append((guild, modo))
        self.letra_prendida = not self.letra_prendida
        return "letra prendida" if self.letra_prendida else "letra apagada"


class FakeRespuesta:
    def __init__(self):
        self.enviados = []

    async def send_message(self, contenido, **kwargs):
        self.enviados.append((contenido, kwargs))


class FakeInteraccion:
    """Quien toca el panel: por defecto está en el mismo canal de voz que el bot."""

    def __init__(self, voz_del_usuario="voz"):
        self.guild = SimpleNamespace(voice_client=SimpleNamespace(channel="voz"))
        self.user = SimpleNamespace(voice=SimpleNamespace(channel=voz_del_usuario) if voz_del_usuario else None)
        self.response = FakeRespuesta()


@pytest.fixture
def view():
    return PlayerPanel(FakeCog(), guild_id=1)


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


def test_estan_los_diez_botones(view):
    assert [b.emoji.name for b in view.children] == [
        "⏯️", "⏭️", "⏹️", "🔁", "🔀", "🔉", "🔊", "📋", "🎙️", "🎤"
    ]


def test_el_boton_de_la_letra_no_se_confunde_con_el_del_dj(view):
    por_emoji = {b.emoji.name: b for b in view.children}

    assert por_emoji["🎤"].label == "Letra" and por_emoji["🎙️"].label is None


def test_ninguna_fila_pasa_de_cinco_botones(view):
    assert max(Counter(b.row for b in view.children).values()) <= 5


async def test_el_boton_de_la_letra_alterna_y_responde_en_privado(view):
    boton = next(b for b in view.children if b.emoji.name == "🎤")
    yo = FakeInteraccion()

    await boton.callback(yo)
    await boton.callback(yo)

    assert view.cog.letra_pedida == [(yo.guild, None)] * 2      # sin modo: alterna
    assert yo.response.enviados == [("letra prendida", {"ephemeral": True}), ("letra apagada", {"ephemeral": True})]


async def test_los_botones_piden_estar_en_el_mismo_canal_de_voz_que_el_bot(view):
    assert await view.interaction_check(FakeInteraccion()) is True

    for afuera in (FakeInteraccion(voz_del_usuario="otro"), FakeInteraccion(voz_del_usuario=None)):
        assert await view.interaction_check(afuera) is False
        assert "mismo canal de voz" in afuera.response.enviados[0][0]
    assert view.cog.letra_pedida == []
