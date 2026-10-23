"""API HTTP local de control: es como los paneles (GTK y web) hablan con el bot.

El bot expone unos pocos endpoints en localhost (estado, log, titulares, letra,
volumen del DJ, refrescar noticias y apagar) y sirve el panel web en `/`; los paneles son
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
from functools import partial
from pathlib import Path

import aiohttp
import wavelink
from aiohttp import web

from core import ajustes, config, lyrics, resolver, tts, voces
from core.feeds import invalidate_all as invalidate_feeds
from core.feeds import refresh_all as refresh_feeds
from core.guionista import CLAUDE, FRASES, NOMBRES, OLLAMA

log =logging.getLogger("api")

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


id_de_pista = resolver.clave_de_pista


def _posicion_ms(bot, vc) -> int:
    """Por dónde va el tema. wavelink arrastra la del tema anterior hasta el primer reporte de Lavalink; el cog ya lo corrige."""
    viva = getattr(bot.get_cog(COG), "posicion_viva", None)
    return int(viva(vc) if viva else getattr(vc, "position", 0) or 0)


def _reproduccion(bot, vc) -> dict:
    """Dónde va la pista en curso, lo que el panel necesita para seguir la letra. `dj`: es un corte del locutor, no una canción."""
    actual = getattr(vc, "current", None)
    dj = bool(getattr(actual, "is_dj_clip", False))
    largo = int(getattr(actual, "length", 0) or 0)
    posicion = _posicion_ms(bot, vc) if actual is not None else 0
    return {
        "pausado": bool(getattr(vc, "paused", False)),
        "dj": dj,
        "pista": id_de_pista(actual) if actual is not None and not dj else None,
        "posicion_ms": min(posicion, largo) if largo else posicion,
        "duracion_ms": largo,
    }


def _estado_voz(bot) -> list[dict]:
    salas = []
    for guild in bot.guilds:
        vc = guild.voice_client
        if not vc:
            continue
        actual = getattr(vc, "current", None)
        salas.append({
            "id": str(guild.id),    # texto: un id de Discord no cabe en un número de JavaScript
            "servidor": guild.name,
            "canal": vc.channel.name if vc.channel else None,
            "sonando": bool(getattr(vc, "playing", False)),
            "cancion": actual.title if actual else None,
            "en_cola": len(vc.queue) if hasattr(vc, "queue") else 0,
            **_reproduccion(bot, vc),
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


def _estado_ytdlp(bot) -> dict:
    """Lo que se sabe de yt-dlp, con la misma forma aunque el bot no tenga resolver.

    El panel web lee `ytdlp.version` sin comprobar que exista: si el campo
    faltara, se rompería en vez de mostrar "no encontrado".
    """
    resolver = getattr(bot, "resolver", None)
    if resolver is None:
        return {
            "version": None, "dias": None, "viejo": False,
            "cliente": None, "cliente_configurado": None,
        }
    return resolver.estado_ytdlp()


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
        "ytdlp": _estado_ytdlp(bot),
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


def _letra_json(motivo: str | None, **campos) -> web.Response:
    """La respuesta de `/api/letra`. Sin letra sale vacía con el `motivo`; `ahora_ms` es la hora del servidor."""
    return web.json_response({
        "servidor": None, "cancion": None, "pista": None, "sincronizada": False, "lineas": [],
        "posicion_ms": 0, "pausado": False, "duracion_ms": 0, "dj": False,
        "ahora_ms": int(time.time() * 1000), "motivo": motivo, **campos,
    })


def _sala_sonando(bot, servidor: str | None):
    """El servidor pedido, o el primero, con algo sonando; None si no hay."""
    for guild in bot.guilds:
        vc = guild.voice_client
        if vc and getattr(vc, "playing", False) and (not servidor or str(guild.id) == servidor):
            return guild
    return None


async def _buscar_letra(bot, titulo: str, autor: str, duracion_ms: int):
    """Busca con la sesión del cog o, si no la hay, con una propia."""
    artista, cancion = lyrics.artist_and_song(titulo, autor)
    session = getattr(bot.get_cog(COG), "_session", None)
    if session is not None:
        return await lyrics.find_synced(session, titulo, artista, cancion, duracion_ms)
    async with aiohttp.ClientSession() as propia:
        return await lyrics.find_synced(propia, titulo, artista, cancion, duracion_ms)


async def handle_letra(request: web.Request) -> web.Response:
    """La letra de lo que suena, con tiempos si los hay. `?servidor=<id>`; sin él, el primero que tenga algo sonando.

    Que no haya letra no es un error: sale vacía con el `motivo` (`nada_sonando`,
    `dj`, `sin_letra`, `cambio` si la pista cambió mientras se buscaba, `error`).
    """
    bot = request.app["bot"]
    guild = _sala_sonando(bot, request.query.get("servidor"))
    if guild is None:
        return _letra_json("nada_sonando")
    vc, base = guild.voice_client, {"servidor": str(guild.id)}
    antes = _reproduccion(bot, vc)
    if antes["dj"] or antes["pista"] is None:
        return _letra_json("dj" if antes["dj"] else "nada_sonando", dj=antes["dj"], **base)
    actual = vc.current
    titulo = resolver.titulo_de(actual)
    buscar = partial(_buscar_letra, bot, titulo, getattr(actual, "author", "") or "", antes["duracion_ms"])
    # La caché es la del cog, la misma de la letra en vivo de Discord: así no se busca dos veces.
    letras = getattr(bot.get_cog(COG), "letras", None) or request.app["letras"]
    try:
        letra = await letras.obtener(antes["pista"], buscar)
    except Exception:
        log.exception("API: error buscando la letra de %r", titulo)
        return _letra_json("error", cancion=titulo, pista=antes["pista"], **base)
    # La búsqueda pudo tardar segundos: la posición se mide después, y si el tema cambió, no vale.
    ahora = _reproduccion(bot, vc)
    if ahora["pista"] != antes["pista"]:
        return _letra_json("cambio", **base)
    datos = {**base, **ahora, "cancion": titulo}
    if letra is None:
        return _letra_json("sin_letra", **datos)
    lineas = [{"ms": ms, "texto": texto} if letra.sincronizada else {"texto": texto} for ms, texto in letra.lineas]
    return _letra_json(None, **datos, sincronizada=letra.sincronizada, lineas=lineas)


async def handle_radio(request: web.Request) -> web.Response:
    """Rearma el autoplay desde lo que suena. `?servidor=<id>`; sin él, el primero que tenga algo sonando."""
    bot = request.app["bot"]
    cog = bot.get_cog(COG)
    if not cog:
        return _sin_cog()
    guild = _sala_sonando(bot, request.query.get("servidor"))
    if guild is None:
        return web.json_response({"ok": False, "aviso": "No hay nada sonando."}, status=409)
    try:
        aviso = await cog.poner_radio(guild)
    except Exception:
        log.exception("API: error armando la radio de %s", guild.id)
        return web.json_response({"ok": False, "aviso": "No pude armar la radio de este tema."}, status=500)
    log.info("API: radio de lo que suena en %s", guild.id)
    return web.json_response({"ok": True, "aviso": aviso})


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


MUESTRA = "Buenas noches, estás escuchando LavaDJ. Esta es mi voz."


def _estado_de_voces() -> dict:
    return {
        "voces": voces.listar(),
        "en_uso": ajustes.obtener("voz"),
        "filtro_radio": ajustes.obtener("filtro_radio"),
        "piper": voces.piper_disponible(),
    }


def _con_voz(request: web.Request, accion) -> web.Response:
    """Aplica `accion(id)` a la voz de `?voz=` y devuelve el estado de las voces."""
    voz_id = request.query.get("voz", "")
    if voz_id not in voces.CATALOGO:
        return web.json_response({"error": f"voz desconocida: {voz_id!r}"}, status=404)
    try:
        accion(voz_id)
    except voces.ErrorDeVoz as error:
        return web.json_response({"error": str(error)}, status=409)
    return web.json_response(_estado_de_voces())


async def handle_voces(request: web.Request) -> web.Response:
    """El catálogo de voces con su estado, la que está en uso y el filtro de radio."""
    return web.json_response(_estado_de_voces())


async def handle_voz(request: web.Request) -> web.Response:
    """Elige la voz del DJ; solo una instalada."""
    return _con_voz(request, voces.elegir)


async def handle_voces_descargar(request: web.Request) -> web.Response:
    """Empieza a bajar una voz en segundo plano; el progreso sale en `/api/voces`."""
    return _con_voz(request, voces.descargas.iniciar)


async def handle_voces_borrar(request: web.Request) -> web.Response:
    return _con_voz(request, voces.borrar)


async def handle_voz_filtro(request: web.Request) -> web.Response:
    """Enciende (`?activo=1`) o apaga (`?activo=0`) el filtro de radio."""
    activo = request.query.get("activo", "")
    if activo not in ("0", "1"):
        return web.json_response({"error": f"activo inválido: {activo!r}"}, status=400)
    try:
        voces.poner_filtro(activo == "1")
    except voces.ErrorDeVoz as error:
        return web.json_response({"error": str(error)}, status=500)
    return web.json_response(_estado_de_voces())


async def handle_voces_muestra(request: web.Request) -> web.Response:
    """Una frase corta con la voz de `?voz=` y el filtro actual, en wav, para escucharla antes de elegirla."""
    voz_id = request.query.get("voz", "")
    if voz_id not in voces.CATALOGO:
        return web.json_response({"error": f"voz desconocida: {voz_id!r}"}, status=404)
    if not voces.instalada(voz_id):
        return web.json_response({"error": "esa voz no está instalada"}, status=409)
    ruta = await tts.synthesize(MUESTRA, voz=voz_id)
    if ruta is None:
        return web.json_response({"error": "no pude sintetizar la muestra"}, status=503)
    try:
        audio = await asyncio.to_thread(ruta.read_bytes)
    finally:
        tts.cleanup(ruta)
    return web.Response(body=audio, content_type="audio/wav")


async def handle_guionista(request: web.Request) -> web.Response:
    """Lee el guionista del DJ; con POST `?nombre=` lo cambia en caliente y guarda la elección."""
    dj = _dj(request)
    if dj is None:
        return _sin_cog()
    guionista = dj.guionista
    session = getattr(request.app["bot"].get_cog(COG), "_session", None)
    if request.method == "POST":
        nombre = request.query.get("nombre", "")
        if nombre not in NOMBRES:
            return web.json_response({"error": f"guionista desconocido: {nombre!r} (hay {', '.join(NOMBRES)})"}, status=400)
        if nombre != FRASES and not (await guionista.disponibles(session))[nombre]:
            return web.json_response({"error": f"{nombre} no está disponible ahora"}, status=409)
        try:
            ajustes.guardar(guionista=nombre)
        except OSError as error:
            return web.json_response({"error": f"no pude guardar el ajuste: {error}"}, status=500)
        guionista.elegir(nombre)
        log.info("API: guionista del DJ a %s", nombre)
    return web.json_response({
        "activo": guionista.activo,
        "disponibles": await guionista.disponibles(session),
        "ultimo_error": guionista.ultimo_error,
        "modelos": {CLAUDE: config.DJ_GUIONISTA_MODELO, OLLAMA: config.OLLAMA_MODELO},
    })


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
    todo = request.query.get("todo") == "1"
    log.info("API: me pidieron cortar la transmisión (%s)%s", motivo, " y bajar Lavalink" if todo else "")
    # Avisamos y devolvemos la respuesta; el cierre ocurre en bot.py, que espera
    # este evento y llama a bot.close(). Matar el proceso aquí dejaría al bot
    # colgado en el canal de voz hasta que Discord se diera cuenta.
    if todo:
        request.app["bot"].apagar_todo = True    # antes del evento: bot.py lo lee al terminar de cerrar
    request.app["apagar"].set()
    return web.json_response({"status": "stopping", "todo": todo})


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
    app["letras"] = lyrics.Letras()
    app.add_routes([
        web.get("/", handle_pagina),
        web.get("/api/log", handle_log),
        web.get("/api/status", handle_status),
        web.get("/api/headlines", handle_headlines),
        web.get("/api/letra", handle_letra),
        web.post("/api/radio", handle_radio),
        web.get("/api/dj_volume", handle_dj_volume),
        web.post("/api/dj_volume", handle_dj_volume),
        web.get("/api/voces", handle_voces),
        web.get("/api/voces/muestra", handle_voces_muestra),
        web.post("/api/voz", handle_voz),
        web.post("/api/voz/filtro", handle_voz_filtro),
        web.post("/api/voces/descargar", handle_voces_descargar),
        web.post("/api/voces/borrar", handle_voces_borrar),
        web.get("/api/guionista", handle_guionista),
        web.post("/api/guionista", handle_guionista),
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
