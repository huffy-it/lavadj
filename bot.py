import asyncio
import logging
import os
import signal

import discord
import wavelink
from discord.ext import commands

from core import config
from core.logging_setup import setup_logging

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
setup_logging(LOG_DIR)
log = logging.getLogger("bot")

# Importar core.tts DESPUÉS de configurar el logging.
from core.api import start_server                          # noqa: E402
from core.resolver import Resolver                          # noqa: E402
from core.feeds import invalidate_all as invalidate_feeds   # noqa: E402
from core.tts import purge_cache as purge_dj_cache          # noqa: E402

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=config.PREFIX, intents=intents)
# Una sola para todo el bot: el cog y los players de cada servidor la comparten,
# así lo que uno aprende de yt-dlp (videos bloqueados, versión) lo ven todos.
bot.resolver = Resolver()


class ServidorNoPermitido(commands.CheckFailure):
    """El comando vino de un servidor fuera de la lista blanca.

    Es una `CheckFailure` para que el check global corte antes de cualquier otro,
    pero `on_command_error` la trata aparte: no se le contesta a nadie.
    """


def servidor_permitido(guild_id: int | None, permitidos: frozenset[int]) -> bool:
    """¿Puede este servidor usar el bot? Lista vacía = cualquiera.

    Sin servidor (`None`, un mensaje directo) siempre pasa: no hay a quién
    restringir, y de lo que se pueda hacer por privado ya se encarga cada comando
    (la música lo rechaza en `cog_check`).
    """
    return guild_id is None or not permitidos or guild_id in permitidos


def avisar_lista_blanca(guilds) -> None:
    """Deja en el log cómo quedó la lista blanca frente a los servidores actuales.

    Solo avisa: no saca al bot de nada. Un ID mal escrito en el `.env` haría que el
    bot abandonara el servidor del dueño, y volver exige reinvitarlo. Salir solo
    se justifica con un servidor nuevo (`on_guild_join`).
    """
    if config.SERVIDORES_INVALIDOS:
        log.warning(
            "SERVIDORES_PERMITIDOS: descarté entradas que no son un ID numérico: %s. "
            "Corrígelas en el .env o ese servidor quedará fuera.",
            ", ".join(config.SERVIDORES_INVALIDOS),
        )
    if not config.SERVIDORES_PERMITIDOS:
        log.warning(
            "SERVIDORES_PERMITIDOS está vacía: el bot acepta cualquier servidor. "
            "Configura SERVIDORES_PERMITIDOS en el .env para limitarlo a los tuyos."
        )
        return
    for guild in guilds:
        if not servidor_permitido(guild.id, config.SERVIDORES_PERMITIDOS):
            log.warning(
                "El servidor %s (ID: %s) no está en SERVIDORES_PERMITIDOS: ahí los "
                "comandos se ignoran. Saca al bot desde Discord o agrega el ID a "
                "la lista.",
                guild.name, guild.id,
            )


@bot.event
async def on_ready():
    log.info('Bot listo: %s (ID: %s)', bot.user, bot.user.id)
    print(f'Bot listo: {bot.user} (ID: {bot.user.id})')
    avisar_lista_blanca(bot.guilds)


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Una invitación nueva a un servidor fuera de la lista: se rechaza yéndose."""
    if servidor_permitido(guild.id, config.SERVIDORES_PERMITIDOS):
        return
    log.warning(
        "Me invitaron a %s (ID: %s), que no está en SERVIDORES_PERMITIDOS: me voy",
        guild.name, guild.id,
    )
    await guild.leave()


@bot.check
async def solo_servidores_permitidos(ctx: commands.Context) -> bool:
    """Check global: en un servidor fuera de la lista no responde ningún comando."""
    if not servidor_permitido(
        ctx.guild.id if ctx.guild else None, config.SERVIDORES_PERMITIDOS
    ):
        raise ServidorNoPermitido()
    return True


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    log.info('Nodo Lavalink conectado: %s', payload.node.uri)
    # `resumed=False` es una sesión nueva: si ya había players, Lavalink los
    # perdió (reinicio del nodo) y el cog tiene que rehacerlos. En el primer
    # arranque no hay ninguno y no pasa nada.
    cog = bot.get_cog("Music")
    if cog and not payload.resumed:
        await cog.recuperar_players(list(payload.node.players.values()))


@bot.event
async def on_wavelink_inactive_player(player: wavelink.Player):
    """Nadie escuchando: se corta.

    Lo dispara wavelink tras varias canciones seguidas sin humanos en el canal.
    Importa sobre todo con el autoplay encendido, que si no seguiría tocando
    para las paredes hasta el fin de los tiempos.
    """
    log.info("Canal vacío en %s: corto la transmisión", player.guild)
    cog = bot.get_cog("Music")
    if cog:
        await cog.teardown(player.guild)
    elif player.connected:
        await player.disconnect()


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Falta un argumento: `{error.param.name}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"Argumento inválido: {error}")
    elif isinstance(error, ServidorNoPermitido):
        # En silencio y antes de la rama de CheckFailure, que sí contesta: a un
        # servidor ajeno no hay nada que explicarle, y contestar sería hacer ruido.
        return
    elif isinstance(error, (commands.NoPrivateMessage, commands.CheckFailure)):
        # El texto lo pone quien rechaza (cog_check, en_el_mismo_canal): son
        # mensajes escritos para que los lea un humano.
        await ctx.send(str(error) or "No puedes usar ese comando aquí.")
    elif not isinstance(error, commands.CommandNotFound):
        raise error


@bot.command(name='refresh')
async def refresh_news(ctx: commands.Context):
    """Vence la caché de titulares: el DJ baja noticias nuevas en el próximo corte."""
    tocadas = invalidate_feeds()
    await ctx.send(
        f"Titulares vencidos ({tocadas} feeds limpios). "
        "El DJ los rebajará en el próximo corte."
    )


async def conectar_lavalink() -> None:
    """Engancha el nodo de audio. Reintenta: el contenedor puede tardar en subir."""
    nodo = wavelink.Node(
        uri=config.LAVALINK_URI,
        password=config.LAVALINK_PASSWORD,
        # Tras estas canciones seguidas sin nadie en el canal, wavelink dispara
        # on_wavelink_inactive_player y cortamos.
        inactive_channel_tokens=config.INACTIVE_CHANNEL_TOKENS,
    )
    for intento in range(1, 11):
        try:
            await wavelink.Pool.connect(client=bot, nodes=[nodo])
            return
        except Exception as error:  # wavelink no expone una excepción común aquí
            log.warning(
                "Lavalink no responde en %s (intento %s/10): %s",
                config.LAVALINK_URI, intento, error,
            )
            await asyncio.sleep(5)
    log.error(
        "Sin nodo Lavalink: la música no va a funcionar. "
        "Levantalo con scripts/lavalink/start-lavalink.sh"
    )


async def esperar_orden_de_apagado(apagar: asyncio.Event) -> None:
    """Cierra el bot cuando lo pide la API (o un SIGTERM), sin cortar de golpe."""
    await apagar.wait()
    log.info("Cerrando: me despido de los canales de voz")
    # El audio lo emite Lavalink, no este proceso: si solo se cierra el bot, el
    # player sigue vivo en el nodo y la música no para. Hay que destruirlo ahí.
    for vc in list(bot.voice_clients):
        try:
            await vc.disconnect()
        except Exception:  # noqa: BLE001 - cerrar tiene que seguir pase lo que pase
            log.exception("No pude desconectar el player de %s", getattr(vc, "guild", "?"))
    await bot.close()


async def main():
    apagar = asyncio.Event()

    loop = asyncio.get_running_loop()
    for señal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(señal, apagar.set)
        except (NotImplementedError, AttributeError):  # pragma: no cover - no-Unix
            log.debug("%s no disponible en esta plataforma", señal)

    api_runner = await start_server(bot, apagar)
    # Después de bindear el puerto, no antes: si ya hay otro bot corriendo, esto
    # muere aquí con "address in use" sin haberle borrado los clips en uso.
    log.info("Caché limpiada: %s del DJ", purge_dj_cache())
    try:
        async with bot:
            await bot.load_extension('cogs.music')
            bot.loop.create_task(conectar_lavalink())
            # yt-dlp saca todo el audio y nadie mira si quedó viejo. Se pregunta
            # una vez al arrancar y queda anotado para que lo muestre el panel.
            bot.loop.create_task(bot.resolver.revisar_ytdlp())
            bot.loop.create_task(esperar_orden_de_apagado(apagar))
            await bot.start(config.DISCORD_TOKEN)
    finally:
        await api_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
