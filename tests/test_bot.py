"""El arranque de bot.py: lo que importa es el orden de las cosas en `main()`."""

import importlib
import sys

import pytest

from core import tts


@pytest.fixture
def bot_mod(monkeypatch):
    """bot.py importado sin tocar el logging de pytest ni la caché real del DJ."""
    llamadas = []
    monkeypatch.setattr("core.logging_setup.setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(tts, "purge_cache", lambda: llamadas.append("purge") or 0)
    sys.modules.pop("bot", None)
    modulo = importlib.import_module("bot")
    modulo.orden = llamadas
    yield modulo
    sys.modules.pop("bot", None)


async def _nada():
    return None


def test_importar_bot_no_borra_la_cache_del_dj(bot_mod):
    """Otra instancia en marcha tiene sus clips en uso: el import no puede tocarlos."""
    assert bot_mod.orden == []


async def test_la_cache_se_purga_solo_despues_de_tomar_el_puerto(bot_mod, monkeypatch):
    class Fin(Exception):
        pass

    async def _start_server(bot, apagar):
        bot_mod.orden.append("puerto")
        return type("Runner", (), {"cleanup": staticmethod(_nada)})()

    async def _load_extension(nombre):
        raise Fin()

    monkeypatch.setattr(bot_mod, "start_server", _start_server)
    monkeypatch.setattr(bot_mod.bot, "load_extension", _load_extension)

    with pytest.raises(Fin):
        await bot_mod.main()

    assert bot_mod.orden == ["puerto", "purge"]


async def test_si_el_puerto_esta_ocupado_no_se_purga_nada(bot_mod, monkeypatch):
    async def _start_server(bot, apagar):
        raise OSError("address in use")

    monkeypatch.setattr(bot_mod, "start_server", _start_server)

    with pytest.raises(OSError):
        await bot_mod.main()

    assert bot_mod.orden == []
