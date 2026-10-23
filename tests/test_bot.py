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

class Fin(Exception):
    pass

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

def _env(monkeypatch, nombre, valor):
    if valor is None:
        monkeypatch.delenv(nombre, raising=False)
    else:
        monkeypatch.setenv(nombre, valor)

def _sin_red(bot_mod, monkeypatch, cargar=_nada):
    """`main()` sin red: anota puerto y cierre, y `bot.start` da un turno al loop y corta con `Fin`."""
    async def _start_server(bot, apagar):
        bot_mod.orden.append("puerto")

        async def cleanup():
            bot_mod.orden.append("limpiar")
        return SimpleNamespace(cleanup=cleanup)

    async def _start(token):
        await asyncio.sleep(0)      # deja correr las tareas que `main()` dejó creadas
        raise Fin()

    monkeypatch.setattr(bot_mod, "start_server", _start_server)
    monkeypatch.setattr(bot_mod, "conectar_lavalink", _nada)
    monkeypatch.setattr(bot_mod, "esperar_orden_de_apagado", lambda apagar: _nada())
    monkeypatch.setattr(bot_mod.bot, "load_extension", lambda nombre: cargar())
    monkeypatch.setattr(bot_mod.bot, "start", _start)

def _ctx(guild_id, cliente=None):
    """Contexto de mentira: `guild_id=None` es un mensaje directo; con `cliente` corre por el camino de `/`."""
    enviados, opciones = [], []

    async def send(texto, **kwargs):
        enviados.append(texto)
        opciones.append(kwargs)

    guild = None if guild_id is None else SimpleNamespace(id=guild_id)
    ctx = SimpleNamespace(guild=guild, send=send, enviados=enviados, opciones=opciones, interaction=None)
    if cliente is not None:
        ctx.interaction = SimpleNamespace(client=cliente, _baton=ctx)
    return ctx

def _servidor(id_, nombre="Servidor"):
    """Servidor de mentira cuyo `leave` anota que lo llamaron."""
    salidas = []

    async def leave():
        salidas.append(id_)

    return SimpleNamespace(id=id_, name=nombre, leave=leave, salidas=salidas)

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

def _nodo(resumed):
    return SimpleNamespace(resumed=resumed, node=SimpleNamespace(uri="http://127.0.0.1:2333", players={}))

class Voz:
    guild = "servidor"

    def __init__(self, orden):
        self.orden = orden

    async def disconnect(self):
        self.orden.append("desconectar")

def _bot_con(bot_mod, monkeypatch, cog, voice_clients=(), close=None):
    falso = SimpleNamespace(get_cog=lambda nombre: cog, voice_clients=list(voice_clients), close=close)
    monkeypatch.setattr(bot_mod, "bot", falso)

class ArbolFalso:
    """Lo único que bot.py le pide al árbol de comandos: copiar lo global a un servidor y sincronizar."""

    def __init__(self, fallan=()):
        self.copiados, self.sincronizados, self.fallan, self.limpiados = [], [], set(fallan), []

    def copy_global_to(self, *, guild):
        self.copiados.append(guild.id)

    def clear_commands(self, *, guild):
        self.limpiados.append(guild)

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

# --- arranque y apagado -------------------------------------------------------

def test_el_import_no_borra_la_cache_del_dj_y_deja_listo_el_resolver(bot_mod):
    """Otra instancia en marcha tiene sus clips en uso; el cog y los players toman el resolver de aquí."""
    assert bot_mod.orden == []
    assert isinstance(bot_mod.bot.resolver, Resolver)

async def test_la_cache_se_purga_solo_despues_de_tomar_el_puerto(bot_mod, monkeypatch):
    async def _fin():
        raise Fin()

    _sin_red(bot_mod, monkeypatch, _fin)
    with pytest.raises(Fin):
        await bot_mod.main()
    assert bot_mod.orden == ["puerto", "purge", "limpiar"]

async def test_si_el_puerto_esta_ocupado_no_se_purga_nada(bot_mod, monkeypatch):
    async def _start_server(bot, apagar):
        raise OSError("address in use")

    monkeypatch.setattr(bot_mod, "start_server", _start_server)
    with pytest.raises(OSError):
        await bot_mod.main()
    assert bot_mod.orden == []

async def test_yt_dlp_se_mantiene_con_el_resolver_del_bot_y_se_cancela_al_cerrar(bot_mod, monkeypatch):
    estado = []

    async def _mantener():
        estado.append("corriendo")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            estado.append("cancelada")
            raise

    _sin_red(bot_mod, monkeypatch)
    monkeypatch.setattr(bot_mod.bot.resolver, "mantener_al_dia", _mantener)
    with pytest.raises(Fin):
        await bot_mod.main()
    assert estado == ["corriendo", "cancelada"]

async def test_lo_guardado_se_retoma_una_sola_vez_y_una_sesion_reanudada_no_retoma(bot_mod, monkeypatch):
    orden = []
    _bot_con(bot_mod, monkeypatch, CogQueAnota(orden))
    await bot_mod.on_wavelink_node_ready(_nodo(True))
    assert orden == []
    for _ in range(3):
        await bot_mod.on_wavelink_node_ready(_nodo(False))
    assert orden == ["recuperar", "retomar", "recuperar", "recuperar"]

async def test_sin_el_cog_cargado_el_nodo_listo_no_falla(bot_mod, monkeypatch):
    _bot_con(bot_mod, monkeypatch, None)
    await bot_mod.on_wavelink_node_ready(_nodo(False))

@pytest.mark.parametrize("con_cog, esperado", [
    (True, ["guardar", "desconectar", "desconectar", "cerrar"]),
    (False, ["desconectar", "desconectar", "cerrar"]),
])
async def test_al_apagar_se_guarda_antes_de_desconectar_los_players(bot_mod, monkeypatch, con_cog, esperado):
    orden = []

    async def _cerrar():
        orden.append("cerrar")

    _bot_con(bot_mod, monkeypatch, CogQueAnota(orden) if con_cog else None, [Voz(orden), Voz(orden)], _cerrar)
    apagar = asyncio.Event()
    apagar.set()
    await bot_mod.esperar_orden_de_apagado(apagar)
    assert orden == esperado

# --- configuración de la lista blanca y de yt-dlp -----------------------------

@pytest.mark.parametrize("crudo, validos, invalidos", [
    (None, set(), ()),
    ("", set(), ()),
    ("123,456,789", {123, 456, 789}, ()),
    ("123 456\t 789", {123, 456, 789}, ()),
    ("123, 456 ,789,, ", {123, 456, 789}, ()),
    ("123,123 123", {123}, ()),
    # Lo que no es número se descarta, pero queda a la vista para el aviso.
    ("123, abc 456 12x -5 +7 4.5 ²", {123, 456}, ("abc", "12x", "-5", "+7", "4.5", "²")),
    ("solo,basura", set(), ("solo", "basura")),
])
def test_la_lista_de_servidores_se_parsea(monkeypatch, crudo, validos, invalidos):
    _env(monkeypatch, "SERVIDORES_PERMITIDOS", crudo)
    resultado, descartados = config._lista_de_ids("SERVIDORES_PERMITIDOS")
    assert isinstance(resultado, frozenset) and resultado == frozenset(validos)
    assert descartados == invalidos

POR_DEFECTO = ("android", "ios")

@pytest.mark.parametrize("crudo, esperado", [
    (None, POR_DEFECTO),
    ("", POR_DEFECTO),
    (" , ,", POR_DEFECTO),
    (" mweb , web ,, ", ("mweb", "web")),
    ("web,mweb,web", ("web", "mweb")),
])
def test_la_lista_de_clientes_se_parsea(monkeypatch, crudo, esperado):
    _env(monkeypatch, "YTDLP_CLIENTES", crudo)
    assert config._lista_de_texto("YTDLP_CLIENTES", POR_DEFECTO) == esperado

@pytest.mark.parametrize("valor, esperado", [(None, True), ("true", True), ("false", False), ("0", False), ("no", False)])
def test_la_actualizacion_automatica_se_apaga_con_false(monkeypatch, valor, esperado):
    _env(monkeypatch, "YTDLP_AUTOACTUALIZAR", valor)
    assert config._booleano("YTDLP_AUTOACTUALIZAR", True) is esperado

# --- lista blanca de servidores -----------------------------------------------

@pytest.mark.parametrize("guild_id, permitidos, esperado", [
    (1, {1, 2}, True), (2, {1, 2}, True), (3, {1, 2}, False), (3, set(), True),
    (None, {1}, True), (None, set(), True),      # sin servidor no hay a quién restringir
])
def test_servidor_permitido(bot_mod, guild_id, permitidos, esperado):
    assert bot_mod.servidor_permitido(guild_id, frozenset(permitidos)) is esperado

@pytest.mark.parametrize("permitidos, guild_id, sale", [({1}, 2, True), ({1}, 1, False), (set(), 2, False)])
async def test_solo_sale_de_un_servidor_nuevo_que_no_esta_en_la_lista(bot_mod, monkeypatch, caplog, permitidos, guild_id, sale):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset(permitidos))
    guild = _servidor(guild_id, "Ajeno")
    with caplog.at_level(logging.WARNING, logger="bot"):
        await bot_mod.on_guild_join(guild)
    assert guild.salidas == ([guild_id] if sale else [])
    assert ("Ajeno (ID: 2)" in caplog.text) is sale

@pytest.mark.parametrize("barra", [False, True])
@pytest.mark.parametrize("permitidos, guild_id, pasa", [({1}, 2, False), ({1}, 1, True), ({1}, None, True), (set(), 2, True)])
async def test_el_check_global_filtra_por_servidor(bot_mod, monkeypatch, barra, permitidos, guild_id, pasa):
    """Con `/`, discord.py corre los checks del bot por otro camino (`HybridAppCommand._check_can_run`)."""
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset(permitidos))
    ctx = _ctx(guild_id, bot_mod.bot if barra else None)
    puede = bot_mod.bot.get_command("refresh").can_run if barra else bot_mod.bot.can_run
    if pasa:
        assert await puede(ctx)
    else:
        with pytest.raises(bot_mod.ServidorNoPermitido):
            await puede(ctx)

def test_el_arranque_avisa_de_la_lista_vacia_y_de_las_entradas_invalidas(bot_mod, monkeypatch, caplog):
    """Un ID mal escrito no puede perderse en silencio, aunque la lista tenga otros."""
    monkeypatch.setattr(config, "SERVIDORES_INVALIDOS", ())
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset())
    with caplog.at_level(logging.WARNING, logger="bot"):
        bot_mod.avisar_lista_blanca([_servidor(1)])
    assert "cualquier servidor" in caplog.text and "SERVIDORES_PERMITIDOS" in caplog.text
    caplog.clear()
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({1}))
    monkeypatch.setattr(config, "SERVIDORES_INVALIDOS", ("12x", "abc"))
    with caplog.at_level(logging.WARNING, logger="bot"):
        bot_mod.avisar_lista_blanca([_servidor(1)])
    assert "12x, abc" in caplog.text and "cualquier servidor" not in caplog.text

def test_el_arranque_avisa_de_los_servidores_ajenos_sin_salirse(bot_mod, monkeypatch, caplog):
    monkeypatch.setattr(config, "SERVIDORES_PERMITIDOS", frozenset({1}))
    monkeypatch.setattr(config, "SERVIDORES_INVALIDOS", ())
    propio, ajeno = _servidor(1, "Propio"), _servidor(2, "Ajeno")
    with caplog.at_level(logging.WARNING, logger="bot"):
        bot_mod.avisar_lista_blanca([propio, ajeno])
    assert "Ajeno" in caplog.text and "(ID: 2)" in caplog.text and "Propio" not in caplog.text
    assert propio.salidas == [] and ajeno.salidas == []     # un ID mal escrito no puede sacar al bot de su servidor

# --- errores de comandos ------------------------------------------------------

@pytest.mark.parametrize("error, texto", [
    (commands.MissingRequiredArgument(SimpleNamespace(name="query", displayed_name=None)), "Falta un argumento: `query`"),
    (commands.BadArgument("no es un número"), "Argumento inválido: no es un número"),
    (commands.CheckFailure("Tienes que estar en el mismo canal de voz que yo."), "Tienes que estar en el mismo canal de voz que yo."),
    (commands.NoPrivateMessage("Los comandos de música funcionan en un servidor."), "Los comandos de música funcionan en un servidor."),
    (commands.CheckFailure(), "No puedes usar ese comando aquí."),
])
async def test_los_avisos_de_error_son_efimeros_con_barra_y_con_prefijo(bot_mod, error, texto):
    for ctx in (_ctx(1, bot_mod.bot), _ctx(1)):
        await bot_mod.on_command_error(ctx, error)
        assert ctx.enviados == [texto] and ctx.opciones == [{"ephemeral": True}]

@pytest.mark.parametrize("barra", [False, True])
async def test_un_servidor_no_permitido_no_recibe_respuesta(bot_mod, barra):
    ctx = _ctx(2, bot_mod.bot if barra else None)
    await bot_mod.on_command_error(ctx, bot_mod.ServidorNoPermitido())
    assert ctx.enviados == []

@pytest.mark.parametrize("barra, enviados", [(True, ["Algo falló de mi lado. Prueba de nuevo."]), (False, [])])
async def test_un_error_inesperado_se_propaga_y_con_barra_avisa_a_quien_escribio(bot_mod, barra, enviados):
    """Sin respuesta, el "pensando…" de una interacción diferida se queda colgado hasta que Discord la da por perdida."""
    ctx = _ctx(1, bot_mod.bot if barra else None)
    with pytest.raises(RuntimeError, match="boom"):
        await bot_mod.on_command_error(ctx, RuntimeError("boom"))
    assert ctx.enviados == enviados
    assert ctx.opciones == [{"ephemeral": True}] * len(enviados)

async def test_si_el_aviso_de_error_tampoco_se_puede_enviar_se_propaga_el_error_original(bot_mod):
    ctx = _ctx(1, bot_mod.bot)

    async def _send_roto(texto, **kwargs):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "no se pudo")

    ctx.send = _send_roto
    with pytest.raises(RuntimeError, match="original"):
        await bot_mod.on_command_error(ctx, RuntimeError("original"))

# --- comandos de barra --------------------------------------------------------

@pytest.mark.parametrize("permitidos, copiados, sincronizados, limpiados, en_log", [
    # Con lista blanca el último sync vacía los globales.
    ({2, 1}, [1, 2], [1, 2, None], [None], ("17 registrados en el servidor 1", "17 registrados en el servidor 2")),
    (set(), [], [None], [], ("17 registrados de forma global", "tardar")),
])
async def test_la_sincronizacion_es_por_servidor_con_lista_blanca_y_global_sin_ella(
        bot_mod, monkeypatch, caplog, permitidos, copiados, sincronizados, limpiados, en_log):
    arbol = ArbolFalso()
    _con_arbol(bot_mod, monkeypatch, arbol, permitidos)
    with caplog.at_level(logging.INFO, logger="bot"):
        await bot_mod.sincronizar_comandos()
    assert (arbol.copiados, arbol.sincronizados, arbol.limpiados) == (copiados, sincronizados, limpiados)
    assert all(fragmento in caplog.text for fragmento in en_log)

async def test_los_comandos_de_barra_se_sincronizan_una_sola_vez_aunque_on_ready_se_repita(bot_mod, monkeypatch):
    arbol = ArbolFalso()
    _con_arbol(bot_mod, monkeypatch, arbol, {1})
    for _ in range(3):
        await bot_mod.on_ready()
    assert arbol.sincronizados == [1, None]

@pytest.mark.parametrize("fallan, permitidos, sincronizados, en_log", [
    ({1}, {1, 2}, [2, None], ("No pude registrar los comandos de barra en el servidor 1", "17 registrados en el servidor 2")),
    ({None}, set(), [], ("No pude registrar los comandos de barra de forma global",)),
    ({None}, {1}, [1], ("No pude borrar los comandos de barra globales",)),
])
async def test_un_error_al_sincronizar_no_tumba_al_bot_ni_a_los_demas_servidores(
        bot_mod, monkeypatch, caplog, fallan, permitidos, sincronizados, en_log):
    arbol = ArbolFalso(fallan)
    _con_arbol(bot_mod, monkeypatch, arbol, permitidos)
    with caplog.at_level(logging.INFO, logger="bot"):
        await bot_mod.sincronizar_comandos()
    assert arbol.sincronizados == sincronizados
    assert all(fragmento in caplog.text for fragmento in en_log)

def test_refresh_tambien_es_un_comando_de_barra(bot_mod):
    comando = bot_mod.bot.get_command("refresh")
    assert isinstance(comando, commands.HybridCommand)
    assert bot_mod.bot.tree.get_command("refresh") is comando.app_command
    assert 1 <= len(comando.app_command.description) <= 100
