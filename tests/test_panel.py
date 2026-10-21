"""El panel no debe pisar métodos de discord.ui.View.

Un handler llamado `stop` reemplaza a View.stop() por un objeto Button, y
View.stop() se llama al borrar el panel y al descargar el cog. Resultado:
'Button' object is not callable, y el botón ⏹️ reventaba en el primer clic.
"""

import discord
import pytest

from core.panel import PlayerPanel


class FakeCog:
    def get_queue(self, guild_id):
        return None


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


def test_estan_los_nueve_botones(view):
    assert [b.emoji.name for b in view.children] == [
        "⏯️", "⏭️", "⏹️", "🔁", "🔀", "🔉", "🔊", "📋", "🎙️"
    ]
