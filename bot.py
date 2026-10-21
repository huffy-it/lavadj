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
from core.resolver import revisar_ytdlp                     # noqa: E402
from core.feeds import invalidate_all as invalidate_feeds   # noqa: E402
from core.tts import purge_cache as purge_dj_cache          # noqa: E402

log.info("Caché limpiada: %s del DJ", purge_dj_cache())

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=config.PREFIX, intents=intents)


@bot.event
async def on_ready():
    log.info('Bot listo: %s (ID: %s)', bot.user, bot.user.id)
    print(f'Bot listo: {bot.user} (ID: {bot.user.id})')


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    log.info('Nodo Lavalink conectado: %s', payload.node.uri)


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
    try:
        async with bot:
            await bot.load_extension('cogs.music')
            bot.loop.create_task(conectar_lavalink())
            # yt-dlp saca todo el audio y nadie mira si quedó viejo. Se pregunta
            # una vez al arrancar y queda anotado para que lo muestre el panel.
            bot.loop.create_task(revisar_ytdlp())
            bot.loop.create_task(esperar_orden_de_apagado(apagar))
            await bot.start(config.DISCORD_TOKEN)
    finally:
        await api_runner.cleanup()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
