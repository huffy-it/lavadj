"""El arranque de bot.py (el orden de las cosas en `main()`) y la lista blanca de servidores."""

import asyncio
import importlib
import logging
import sys
from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands

from core import config, tts
from core.resolver import Resolver


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


def test_el_bot_trae_su_resolver_desde_el_import(bot_mod):
    """El cog y los players lo toman de aquí: tiene que existir antes de cargar nada."""
    assert isinstance(bot_mod.bot.resolver, Resolver)


def _arrancar_sin_red(bot_mod, monkeypatch, durante_el_arranque):
    """`main()` sin red: `bot.start` da un turno al loop, llama a `durante_el_arranque` y corta con `Fin`."""
    async def _start_server(bot, apagar):
        return type("Runner", (), {"cleanup": staticmethod(_nada)})()

    async def _load_extension(nombre):
        return None

    async def _start(token):
        await asyncio.sleep(0)      # deja correr las tareas que `main()` dejó creadas
        await durante_el_arranque()
        raise Fin()

    monkeypatch.setattr(bot_mod, "start_server", _start_server)
    monkeypatch.setattr(bot_mod, "conectar_lavalink", _nada)
    monkeypatch.setattr(bot_mod, "esperar_orden_de_apagado", lambda apagar: _nada())
    monkeypatch.setattr(bot_mod.bot, "load_extension", _load_extension)
    monkeypatch.setattr(bot_mod.bot, "start", _start)


class Fin(Exception):
    pass


async def test_al_arrancar_se_mantiene_yt_dlp_con_el_resolver_del_bot(bot_mod, monkeypatch):
    mantenidas = []

    async def _mantener():
        mantenidas.append("yt-dlp")
        await asyncio.sleep(3600)

    _arrancar_sin_red(bot_mod, monkeypatch, _nada)
    monkeypatch.setattr(bot_mod.bot.resolver, "mantener_al_dia", _mantener)
    with pytest.raises(Fin):
        await bot_mod.main()
    assert mantenidas == ["yt-dlp"]


async def test_al_cerrar_el_bot_se_cancela_el_mantenimiento_de_yt_dlp(bot_mod, monkeypatch):
    estado = []

    async def _mantener():
        estado.append("corriendo")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            estado.append("cancelada")
            raise

    _arrancar_sin_red(bot_mod, monkeypatch, _nada)
    monkeypatch.setattr(bot_mod.bot.resolver, "mantener_al_dia", _mantener)
    with pytest.raises(Fin):
        await bot_mod.main()
    assert estado == ["corriendo", "cancelada"]


async def test_cerrar_sin_haber_llegado_a_lanzar_el_mantenimiento_no_estorba(bot_mod, monkeypatch):
    async def _start_server(bot, apagar):
        return type("Runner", (), {"cleanup": staticmethod(_nada)})()

    async def _load_extension(nombre):
        raise Fin()

    monkeypatch.setattr(bot_mod, "start_server", _start_server)
    monkeypatch.setattr(bot_mod.bot, "load_extension", _load_extension)
    with pytest.raises(Fin):
        await bot_mod.main()


# --- la cola en disco: retomarla al arrancar y guardarla al cerrar ------------

class CogQueAnota:
    """De cada llamada que bot.py le hace al cog, solo importa el orden."""

    def __init__(self, orden):
        self.orden = orden

    async def recuperar_players(self, players):
        self.orden.append("recuperar")

    async def retomar_guardadas(self):
        self.orden.append("retomar")

    async def guardar_y_cerrar(self):
        self.orden.append("guardar")


def _nodo_listo(resumed):
    return SimpleNamespace(resumed=resumed, node=SimpleNamespace(uri="http://127.0.0.1:2333", players={}))


def _bot_con(bot_mod, monkeypatch, cog, voice_clients=(), close=None):
    falso = SimpleNamespace(get_cog=lambda nombre: cog, voice_clients=list(voice_clients), close=close)
    monkeypatch.setattr(bot_mod, "bot", falso)


async def test_lo_guardado_se_retoma_una_sola_vez_aunque_el_nodo_reconecte(bot_mod, monkeypatch):
    orden = []
    _bot_con(bot_mod, monkeypatch, CogQueAnota(orden))

    for _ in range(3):
        await bot_mod.on_wavelink_node_ready(_nodo_listo(resumed=False))

    assert orden == ["recuperar", "retomar", "recuperar", "recuperar"]


async def test_una_sesion_reanudada_no_retoma_nada_pero_la_primera_nueva_si(bot_mod, monkeypatch):
    orden = []
    _bot_con(bot_mod, monkeypatch, CogQueAnota(orden))

    await bot_mod.on_wavelink_node_ready(_nodo_listo(resumed=True))
    assert orden == []

    await bot_mod.on_wavelink_node_ready(_nodo_listo(resumed=False))
    assert orden == ["recuperar", "retomar"]


async def test_sin_el_cog_cargado_el_nodo_listo_no_falla(bot_mod, monkeypatch):
    _bot_con(bot_mod, monkeypatch, None)

    await bot_mod.on_wavelink_node_ready(_nodo_listo(resumed=False))


async def test_al_apagar_se_guarda_antes_de_desconectar_los_players(bot_mod, monkeypatch):
    orden = []

    class Voz:
        guild = "servidor"

        async def disconnect(self):
            orden.append("desconectar")

    async def _cerrar():
        orden.append("cerrar")

    _bot_con(bot_mod, monkeypatch, CogQueAnota(orden), [Voz(), Voz()], _cerrar)
    apagar = asyncio.Event()
    apagar.set()

    await bot_mod.esperar_orden_de_apagado(apagar)

    assert orden == ["guardar", "desconectar", "desconectar", "cerrar"]


async def test_al_apagar_sin_el_cog_cargado_igual_se_desconecta_y_se_cierra(bot_mod, monkeypatch):
    orden = []

    class Voz:
        guild = "servidor"

        async def disconnect(self):
            orden.append("desconectar")

    async def _cerrar():
        orden.append("cerrar")

    _bot_con(bot_mod, monkeypatch, None, [Voz()], _cerrar)
    apagar = asyncio.Event()
    apagar.set()

    await bot_mod.esperar_orden_de_apagado(apagar)

    assert orden == ["desconectar", "cerrar"]


# --- Lista blanca de servidores ----------------------------------------------

def _servidor(id_, nombre="Servidor"):
    """Un servidor de mentira cuyo `leave` anota que lo llamaron."""
    salidas = []

    async def leave():
        salidas.append(id_)

    return SimpleNamespace(id=id_, name=nombre, leave=leave, salidas=salidas)


def _ctx(guild_id):
    """Un contexto de mentira: `guild_id=None` es un mensaje directo."""
    enviados, opciones = [], []

    async def send(texto, **kwargs):
        enviados.append(texto)
        opciones.append(kwargs)

    guild = None if guild_id is None else SimpleNamespace(id=guild_id)
    return SimpleNamespace(guild=guild, send=send, enviados=enviados, opciones=opciones, interaction=None)


def _ctx_de_barra(bot_mod, guild_id):
    """Como `_ctx`, pero con el `interaction` que hace que discord.py corra los checks por el camino de `/`."""
    ctx = _ctx(guild_id)
    ctx.interaction = SimpleNamespace(client=bot_mod.bot)
    ctx.interaction._baton = ctx
    return ctx


@pytest.mark.parametrize("crudo, validos, invalidos", [
    ("", set(), ()),
    ("   ", set(), ()),
    ("123", {123}, ()),
    ("123,456,789", {123, 456, 789}, ()),
    ("123 456  789", {123, 456, 789}, ()),
    ("123, 456 ,789,, ", {123, 456, 789}, ()),
    ("123\t456", {123, 456}, ()),
    ("123,123 123", {123}, ()),
    # Lo que no es número se descarta, pero queda a la vista para el aviso.
    ("123, abc 456 12x -5 +7 4.5 ²", {123, 456}, ("abc", "12x", "-5", "+7", "4.5", "²")),
    ("solo,basura", set(), ("solo", "basura")),
])
def test_la_lista_de_servidores_se_parsea(monkeypatch, crudo, validos, invalidos):
    monkeypatch.setenv("SERVIDORES_PERMITIDOS", crudo)

    resultado, descartados = config._lista_de_ids("SERVIDORES_PERMITIDOS")

    assert resultado == frozenset(validos)
    assert isinstance(resultado, frozenset)
    assert descartados == invalidos


def test_sin_la_variable_no_hay_restriccion(monkeypatch):
    """Quien no la tiene en su `.env` sigue como antes: lista vacía y nada que avisar."""
    monkeypatch.delenv("SERVIDORES_PERMITIDOS", raising=False)

    assert config._lista_de_ids("SERVIDORES_PERMITIDOS") == (frozenset(), ())


POR_DEFECTO = ("android", "ios")


@pytest.mark.parametrize("crudo, esperado", [
    ("mweb,web", ("mweb", "web")),
    (" mweb , web ,, ", ("mweb", "web")),
    ("web,mweb,web", ("web", "mweb")),
    ("", POR_DEFECTO),
    (" , ,", POR_DEFECTO),
])
def test_la_lista_de_clientes_se_parsea(monkeypatch, crudo, esperado):
    monkeypatch.setenv("YTDLP_CLIENTES", crudo)
    assert config._lista_de_texto("YTDLP_CLIENTES", POR_DEFECTO) == esperado


def test_sin_la_variable_de_clientes_se_usan_los_de_siempre(monkeypatch):
    monkeypatch.delenv("YTDLP_CLIENTES", raising=False)
    assert config._lista_de_texto("YTDLP_CLIENTES", POR_DEFECTO) == POR_DEFECTO


@pytest.mark.parametrize("valor, esperado", [
    (None, True),
    ("true", True),
    ("false", False),
    ("0", False),
    ("no", False),
])
def test_la_actualizacion_automatica_se_apaga_con_false(monkeypatch, valor, esperado):
    if valor is None:
        monkeypatch.delenv("YTDLP_AUTOACTUALIZAR", raising=False)
    else:
        monkeypatch.setenv("YTDLP_AUTOACTUALIZAR", valor)
    assert config._booleano("YTDLP_AUTOACTUALIZAR", True) is esperado


def test_servidor_permitido(bot_mod):
    permitidos = frozenset({1, 2})

    assert bot_mod.servidor_permitido(1, permitidos)
    assert bot_mod.servidor_permitido(2, permitidos)
    assert not bot_mod.servidor_permitido(3, permitidos)


def test_lista_vacia_permite_cualquier_servidor(bot_mod):
    assert bot_mod.servidor_permitido(3, frozenset())


def test_los_mensajes_directos_no_se_restringen(bot_mod):
    """Sin servidor no hay a quién restringir, con lista o sin ella."""
    assert bot_mod.servidor_permitido(None, frozenset({1}))
    assert bot_mod.servidor_permitido(None, frozenset())


async def test_sale_de_un_servidor_nuevo_que_no_esta_en_la_lista(bot_mod, monkeypatch, caplog):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({1}))
    guild = _servidor(2, "Ajeno")

    with caplog.at_level(logging.WARNING, logger="bot"):
        await bot_mod.on_guild_join(guild)

    assert guild.salidas == [2]
    assert "Ajeno" in caplog.text and "2" in caplog.text


async def test_no_sale_de_un_servidor_de_la_lista(bot_mod, monkeypatch):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({1}))
    guild = _servidor(1)

    await bot_mod.on_guild_join(guild)

    assert guild.salidas == []


async def test_con_la_lista_vacia_no_sale_de_ningun_servidor(bot_mod, monkeypatch):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset())
    guild = _servidor(2)

    await bot_mod.on_guild_join(guild)

    assert guild.salidas == []


async def test_el_check_rechaza_los_comandos_de_un_servidor_ajeno(bot_mod, monkeypatch):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({1}))

    with pytest.raises(bot_mod.ServidorNoPermitido):
        await bot_mod.bot.can_run(_ctx(2))


async def test_el_check_deja_pasar_servidores_de_la_lista_y_mensajes_directos(bot_mod, monkeypatch):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({1}))

    assert await bot_mod.bot.can_run(_ctx(1))
    assert await bot_mod.bot.can_run(_ctx(None))


async def test_el_check_no_restringe_con_la_lista_vacia(bot_mod, monkeypatch):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset())

    assert await bot_mod.bot.can_run(_ctx(2))


async def test_un_servidor_no_permitido_no_recibe_respuesta(bot_mod):
    ctx = _ctx(2)

    await bot_mod.on_command_error(ctx, bot_mod.ServidorNoPermitido())

    assert ctx.enviados == []


async def test_otros_check_failure_siguen_contestando(bot_mod):
    """La excepción nueva es una `CheckFailure`: las demás no pueden quedar mudas."""
    ctx = _ctx(1)

    await bot_mod.on_command_error(ctx, commands.CheckFailure("Entra a un canal de voz."))

    assert ctx.enviados == ["Entra a un canal de voz."]


def test_on_ready_avisa_si_la_lista_esta_vacia(bot_mod, monkeypatch, caplog):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset())
    monkeypatch.setattr(config, "SERVIDORES_INVALIDOS", ())

    with caplog.at_level(logging.WARNING, logger="bot"):
        bot_mod.avisar_lista_blanca([_servidor(1)])

    assert "cualquier servidor" in caplog.text
    assert "SERVIDORES_PERMITIDOS" in caplog.text


def test_on_ready_avisa_de_las_entradas_invalidas(bot_mod, monkeypatch, caplog):
    """Un ID mal escrito no puede perderse en silencio, aunque la lista tenga otros."""
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({1}))
    monkeypatch.setattr(config, "SERVIDORES_INVALIDOS", ("12x", "abc"))

    with caplog.at_level(logging.WARNING, logger="bot"):
        bot_mod.avisar_lista_blanca([_servidor(1)])

    assert "12x, abc" in caplog.text
    assert "cualquier servidor" not in caplog.text


def test_on_ready_avisa_de_los_servidores_ajenos_sin_salirse(bot_mod, monkeypatch, caplog):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({1}))
    monkeypatch.setattr(config, "SERVIDORES_INVALIDOS", ())
    propio, ajeno = _servidor(1, "Propio"), _servidor(2, "Ajeno")

    with caplog.at_level(logging.WARNING, logger="bot"):
        bot_mod.avisar_lista_blanca([propio, ajeno])

    assert "Ajeno" in caplog.text and "(ID: 2)" in caplog.text
    assert "Propio" not in caplog.text
    # Un ID mal escrito no puede hacer que el bot abandone el servidor del dueño.
    assert propio.salidas == [] and ajeno.salidas == []


# --- comandos de barra ---------------------------------------------------------

class ArbolFalso:
    """Lo único que bot.py le pide al árbol de comandos: copiar lo global a un servidor y sincronizar."""

    def __init__(self, fallan=()):
        self.copiados, self.sincronizados, self.fallan = [], [], set(fallan)

    def copy_global_to(self, *, guild):
        self.copiados.append(guild.id)

    async def sync(self, *, guild=None):
        destino = guild.id if guild else None
        if destino in self.fallan:
            raise RuntimeError("403 Missing Access")
        self.sincronizados.append(destino)
        return [object()] * 17


def _con_arbol(bot_mod, monkeypatch, arbol, permitidos=()):
    monkeypatch.setattr(bot_mod, "bot", SimpleNamespace(tree=arbol, user=SimpleNamespace(id=1), guilds=[]))
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset(permitidos))
    monkeypatch.setattr(config, "SERVIDORES_INVALIDOS", ())


async def test_con_lista_blanca_se_copia_y_se_sincroniza_en_cada_servidor(bot_mod, monkeypatch, caplog):
    arbol = ArbolFalso()
    _con_arbol(bot_mod, monkeypatch, arbol, permitidos={2, 1})

    with caplog.at_level(logging.INFO, logger="bot"):
        await bot_mod.sincronizar_comandos()

    assert arbol.copiados == [1, 2]
    assert arbol.sincronizados == [1, 2]
    assert "17 registrados en el servidor 1" in caplog.text and "17 registrados en el servidor 2" in caplog.text


async def test_sin_lista_blanca_la_sincronizacion_es_global(bot_mod, monkeypatch, caplog):
    arbol = ArbolFalso()
    _con_arbol(bot_mod, monkeypatch, arbol)

    with caplog.at_level(logging.INFO, logger="bot"):
        await bot_mod.sincronizar_comandos()

    assert arbol.copiados == []
    assert arbol.sincronizados == [None]
    assert "17 registrados de forma global" in caplog.text and "tardar" in caplog.text


async def test_los_comandos_de_barra_se_sincronizan_una_sola_vez_aunque_on_ready_se_repita(bot_mod, monkeypatch):
    arbol = ArbolFalso()
    _con_arbol(bot_mod, monkeypatch, arbol, permitidos={1})

    for _ in range(3):
        await bot_mod.on_ready()

    assert arbol.sincronizados == [1]


async def test_un_error_al_sincronizar_no_tumba_al_bot_ni_a_los_demas_servidores(bot_mod, monkeypatch, caplog):
    arbol = ArbolFalso(fallan={1})
    _con_arbol(bot_mod, monkeypatch, arbol, permitidos={1, 2})

    with caplog.at_level(logging.INFO, logger="bot"):
        await bot_mod.sincronizar_comandos()

    assert arbol.sincronizados == [2]
    assert "No pude registrar los comandos de barra en el servidor 1" in caplog.text
    assert "17 registrados en el servidor 2" in caplog.text


async def test_un_error_en_la_sincronizacion_global_tampoco_tumba_al_bot(bot_mod, monkeypatch, caplog):
    _con_arbol(bot_mod, monkeypatch, ArbolFalso(fallan={None}))

    with caplog.at_level(logging.INFO, logger="bot"):
        await bot_mod.on_ready()

    assert "No pude registrar los comandos de barra de forma global" in caplog.text


def test_refresh_tambien_es_un_comando_de_barra(bot_mod):
    comando = bot_mod.bot.get_command("refresh")

    assert isinstance(comando, commands.HybridCommand)
    assert bot_mod.bot.tree.get_command("refresh") is comando.app_command
    assert 1 <= len(comando.app_command.description) <= 100


async def test_el_check_global_tambien_corta_un_comando_de_barra_de_un_servidor_ajeno(bot_mod, monkeypatch):
    """Con `/`, discord.py corre los checks del bot por otro camino (`HybridAppCommand._check_can_run`)."""
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({1}))
    refresh = bot_mod.bot.get_command("refresh")

    with pytest.raises(bot_mod.ServidorNoPermitido):
        await refresh.can_run(_ctx_de_barra(bot_mod, 2))

    assert await refresh.can_run(_ctx_de_barra(bot_mod, 1))
    assert await refresh.can_run(_ctx_de_barra(bot_mod, None))


@pytest.mark.parametrize("error", [
    commands.MissingRequiredArgument(SimpleNamespace(name="query", displayed_name=None)),
    commands.BadArgument("no es un número"),
    commands.CheckFailure("Tienes que estar en el mismo canal de voz que yo."),
    commands.NoPrivateMessage("Los comandos de música funcionan en un servidor."),
])
async def test_los_avisos_de_error_son_efimeros_con_barra_y_normales_con_prefijo(bot_mod, error):
    de_barra = _ctx_de_barra(bot_mod, 1)
    con_prefijo = _ctx(1)

    await bot_mod.on_command_error(de_barra, error)
    await bot_mod.on_command_error(con_prefijo, error)

    assert de_barra.enviados == con_prefijo.enviados and len(de_barra.enviados) == 1
    assert de_barra.opciones == [{"ephemeral": True}]


async def test_un_servidor_no_permitido_sigue_mudo_tambien_con_barra(bot_mod):
    ctx = _ctx_de_barra(bot_mod, 2)

    await bot_mod.on_command_error(ctx, bot_mod.ServidorNoPermitido())

    assert ctx.enviados == []


async def test_un_error_inesperado_con_barra_avisa_a_quien_escribio_y_se_propaga(bot_mod):
    """Sin respuesta, el "pensando…" de una interacción diferida se queda colgado hasta que Discord la da por perdida."""
    ctx = _ctx_de_barra(bot_mod, 1)

    with pytest.raises(RuntimeError, match="boom"):
        await bot_mod.on_command_error(ctx, RuntimeError("boom"))

    assert ctx.enviados == ["Algo falló de mi lado. Prueba de nuevo."]
    assert ctx.opciones == [{"ephemeral": True}]


async def test_un_error_inesperado_con_prefijo_se_propaga_sin_decir_nada(bot_mod):
    ctx = _ctx(1)

    with pytest.raises(RuntimeError, match="boom"):
        await bot_mod.on_command_error(ctx, RuntimeError("boom"))

    assert ctx.enviados == []


async def test_si_el_aviso_de_error_tampoco_se_puede_enviar_se_propaga_el_error_original(bot_mod):
    ctx = _ctx_de_barra(bot_mod, 1)

    async def _send_roto(texto, **kwargs):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "no se pudo")

    ctx.send = _send_roto

    with pytest.raises(RuntimeError, match="original"):
        await bot_mod.on_command_error(ctx, RuntimeError("original"))
