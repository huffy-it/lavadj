"""API HTTP local de control: es como los paneles (GTK y web) hablan con el bot.

El bot expone unos pocos endpoints en localhost (estado, log, titulares, volumen
del DJ, refrescar noticias y apagar) y sirve el panel web en `/`; los paneles son
clientes HTTP comunes y corrientes.

Solo escucha en 127.0.0.1: no hay autenticación a propósito, porque nada de
fuera de la máquina llega hasta aquí. Eso no basta contra una página web que
abras en el navegador: puede mandar un formulario a 127.0.0.1 o usar DNS
rebinding. Por eso un middleware valida `Host` y `Origin` (ver `proteger`).
"""

import asyncio
import logging
import time
from collections import deque
from pathlib import Path

import wavelink
from aiohttp import web

from core import config, resolver
from core.feeds import invalidate_all as invalidate_feeds
from core.feeds import refresh_all as refresh_feeds

log = logging.getLogger("api")

HOST = config.API_HOST

PAGINA = Path(__file__).parent / "static" / "panel.html"
LOG_FILE = config.PROJECT_ROOT / "logs" / "bot.log"

# El cog de música es el dueño del DJ y de la caché de titulares.
COG = "Music"


def _dj(request: web.Request):
    """El DJ del bot, o None si el cog todavía no cargó."""
    cog = request.app["bot"].get_cog(COG)
    return getattr(cog, "dj", None) if cog else None


def _sin_cog() -> web.Response:
    return web.json_response(
        {"error": "el bot todavía no cargó el cog de música"}, status=503
    )


def _estado_lavalink() -> dict:
    """Si el nodo de audio está conectado. Sin esto no suena nada."""
    nodos = list(wavelink.Pool.nodes.values())
    if not nodos:
        return {"conectado": False, "uri": None}
    nodo = nodos[0]
    return {
        "conectado": nodo.status is wavelink.NodeStatus.CONNECTED,
        "uri": nodo.uri,
    }


def _estado_voz(bot) -> list[dict]:
    salas = []
    for guild in bot.guilds:
        vc = guild.voice_client
        if not vc:
            continue
        actual = getattr(vc, "current", None)
        salas.append({
            "servidor": guild.name,
            "canal": vc.channel.name if vc.channel else None,
            "sonando": bool(getattr(vc, "playing", False)),
            "cancion": actual.title if actual else None,
            "en_cola": len(vc.queue) if hasattr(vc, "queue") else 0,
        })
    return salas


def _al_aire_y_con_cola(bot) -> tuple[bool, bool]:
    """Si el bot está en un canal de voz y si le queda algo por tocar.

    Las dos cosas juntas son lo que separa "no suena nada porque nadie pidió
    nada" de "no suena nada y debería".
    """
    al_aire = False
    hay_cola = False
    for guild in getattr(bot, "guilds", []):
        vc = getattr(guild, "voice_client", None)
        if not vc:
            continue
        al_aire = True
        hay_cola = hay_cola or bool(getattr(vc, "queue", None) or getattr(vc, "auto_queue", None))
    return al_aire, hay_cola


def _estado_salud(bot) -> dict | None:
    """Cómo viene la transmisión. None si el cog todavía no cargó."""
    cog = bot.get_cog(COG)
    salud = getattr(cog, "salud", None) if cog else None
    if salud is None:
        return None
    al_aire, hay_cola = _al_aire_y_con_cola(bot)
    return salud.estado(al_aire=al_aire, hay_cola=hay_cola)


async def handle_status(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    listo = bot.is_ready()
    return web.json_response({
        "status": "running",
        "uptime": time.monotonic() - request.app["arranque"],
        "listo": listo,
        "usuario": str(bot.user) if bot.user else None,
        "servidores": len(bot.guilds) if listo else 0,
        "lavalink": _estado_lavalink(),
        "voz": _estado_voz(bot) if listo else [],
        # Que el proceso conteste no quiere decir que esté sonando algo: el bot
        # se puede quedar mudo sin caerse, y sin esto el panel no lo nota.
        "salud": _estado_salud(bot),
        "ytdlp": resolver.estado_ytdlp(),
    })


def _foto_de_titulares(dj) -> dict:
    """Lo que hay en la caché de noticias, listo para el panel."""
    cache = getattr(dj, "feeds", None) if dj else None
    if not cache:
        # Sin dj_feeds.json no hay caché que mirar. No es un error: el DJ
        # funciona igual, solo que sin noticias.
        return {"titulares": [], "edad": None, "fuentes": 0}
    return cache.snapshot()


async def handle_refresh(request: web.Request) -> web.Response:
    dj = _dj(request)
    cog = request.app["bot"].get_cog(COG)
    session = getattr(cog, "_session", None) if cog else None

    if session is None:
        # Sin sesión HTTP no se puede bajar nada; al menos se vence lo que hay
        # para que el próximo corte del DJ traiga noticias frescas.
        tocadas = invalidate_feeds()
        log.info("API: titulares vencidos sin rebajar (%s caché/s)", tocadas)
    else:
        tocadas = await refresh_feeds(session)
        log.info("API: titulares rebajados (%s caché/s)", tocadas)

    return web.json_response({"status": "ok", "cleared": tocadas, **_foto_de_titulares(dj)})


async def handle_headlines(request: web.Request) -> web.Response:
    """Los titulares que el DJ tiene esperando salir al aire."""
    cog = request.app["bot"].get_cog(COG)
    if not cog:
        return _sin_cog()
    return web.json_response(_foto_de_titulares(cog.dj))


async def handle_dj_volume(request: web.Request) -> web.Response:
    """Lee o mueve el volumen de la voz del DJ (0-200%)."""
    dj = _dj(request)
    if dj is None:
        return _sin_cog()

    if request.method == "POST":
        crudo = request.query.get("valor", "")
        try:
            pedido = int(crudo)
        except ValueError:
            return web.json_response(
                {"error": f"volumen inválido: {crudo!r}"}, status=400
            )
        log.info("API: volumen del DJ a %s%%", dj.set_volume(pedido))

    return web.json_response({"volumen": dj.volume, "disponible": dj.available()})


async def handle_pagina(request: web.Request) -> web.Response:
    """El panel web: la alternativa multiplataforma al panel GTK."""
    return web.Response(text=PAGINA.read_text(encoding="utf-8"), content_type="text/html")


async def handle_log(request: web.Request) -> web.Response:
    """Las últimas líneas de bot.log, para verlas desde el panel web."""
    try:
        n = max(1, min(500, int(request.query.get("lineas", "60"))))
    except ValueError:
        n = 60
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as fh:
            lineas = [linea.rstrip("\n") for linea in deque(fh, maxlen=n)]
    except OSError:
        lineas = []
    return web.json_response({"lineas": lineas})


async def handle_stop(request: web.Request) -> web.Response:
    # Quién lo pidió queda en el log: si la transmisión se corta sola, esto es
    # lo primero que hay que poder mirar.
    motivo = request.query.get("motivo") or "sin motivo declarado"
    log.info("API: me pidieron cortar la transmisión (%s)", motivo)
    # Avisamos y devolvemos la respuesta; el cierre ocurre en bot.py, que espera
    # este evento y llama a bot.close(). Matar el proceso aquí dejaría al bot
    # colgado en el canal de voz hasta que Discord se diera cuenta.
    request.app["apagar"].set()
    return web.json_response({"status": "stopping"})


def _anfitriones_locales(puerto: int) -> frozenset[str]:
    return frozenset(f"{nombre}:{puerto}" for nombre in ("127.0.0.1", "localhost", "[::1]"))


def proteger(puerto: int):
    """Middleware contra CSRF y DNS rebinding.

    - `Host`: una página con DNS rebinding resuelve su dominio a 127.0.0.1, pero
      el navegador sigue mandando `Host: dominio-malo:puerto`. Si no es
      uno de los nuestros, 403 (también para los GET, que es donde se filtraría
      el estado y el log).
    - `Origin`: un formulario de cualquier página puede hacer POST a localhost y
      CORS no lo frena. El navegador siempre manda `Origin` en esos POST, así
      que si viene y no es el nuestro (o es `null`), 403. Sin `Origin` (urllib
      del panel GTK, curl) pasa: no es un navegador cruzando orígenes.

    El puerto se pasa explícito (el de `config.API_PORT`, o el real en tests)
    en vez de leerlo de la conexión, que detrás de un reenvío mentiría.
    """
    anfitriones = _anfitriones_locales(puerto)
    origenes = frozenset(f"http://{a}" for a in anfitriones)

    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.headers.get("Host", "").lower() not in anfitriones:
            log.warning("API: Host rechazado (%r)", request.headers.get("Host"))
            return web.json_response({"error": "host no permitido"}, status=403)
        if request.method not in ("GET", "HEAD"):
            origen = request.headers.get("Origin")
            if origen is not None and origen.lower() not in origenes:
                log.warning("API: Origin rechazado (%r) en %s %s", origen, request.method, request.path)
                return web.json_response({"error": "origen no permitido"}, status=403)
        return await handler(request)

    return middleware


def crear_app(bot, apagar: asyncio.Event, puerto: int) -> web.Application:
    app = web.Application(middlewares=[proteger(puerto)])
    app["bot"] = bot
    app["apagar"] = apagar
    app["arranque"] = time.monotonic()
    app.add_routes([
        web.get("/", handle_pagina),
        web.get("/api/log", handle_log),
        web.get("/api/status", handle_status),
        web.get("/api/headlines", handle_headlines),
        web.get("/api/dj_volume", handle_dj_volume),
        web.post("/api/dj_volume", handle_dj_volume),
        web.post("/api/refresh_news", handle_refresh),
        web.post("/api/stop", handle_stop),
    ])
    return app


async def start_server(bot, apagar: asyncio.Event) -> web.AppRunner:
    """Levanta la API y devuelve el runner para poder cerrarlo al salir."""
    app = crear_app(bot, apagar, config.API_PORT)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, config.API_PORT).start()
    log.info("API HTTP de control escuchando en http://%s:%s", HOST, config.API_PORT)
    return runner
