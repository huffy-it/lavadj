import asyncio
import logging
import time

import aiohttp
import discord
from discord.ext import commands
import wavelink

from core import config, controller, lyrics, resolver, spotify, tts
from core.dj import DJ
from core.feeds import FeedCache
from core.messages import split_message
from core.panel import PlayerPanel, build_embed
from core.salud import Salud

log = logging.getLogger("music")

IDLE_TIMEOUT = config.IDLE_TIMEOUT

# Cuántos temas de Spotify se buscan a la vez en YouTube. De a uno una lista de
# 50 tarda una eternidad; todos juntos es una avalancha para Lavalink.
SPOTIFY_LOTE = 5

# Cuántas veces se vuelve a intentar una pista que Lavalink no pudo resolver.
# YouTube le contesta "Invalid status code for player api response: 400" al
# cliente ANDROID de a ratos, sin patrón: el mismo video que se cae ahora suele
# sonar al segundo intento. Sin esto un tropiezo se lleva puesta la canción, y
# en una lista larga el autoplay va saltando y las quema todas de corrido.
REINTENTOS_POR_PISTA = 2

# Cuántos temas seguidos se pueden perder (agotados sus reintentos) antes de dar
# la transmisión por caída y dejar de avanzar la cola. Con YouTube sin entregar
# audio y el autoplay encendido, sin este tope se pasaría el día probando temas.
CAIDAS_SEGUIDAS_MAX = 3

# El tope fijo de wavelink: con su cuenta de fallos ahí, deja de avanzar la cola.
TOPE_DE_WAVELINK = 3

# Lo que se avisa cuando el nodo de audio se reinició y no hubo manera de retomar.
AVISO_NODO_REINICIADO = (
    "El nodo de audio se reinició y no pude retomar la transmisión: "
    "la corté. Usa `!play` para volver a empezar."
)


def en_el_mismo_canal():
    """Los controles son de quien está escuchando, no de cualquiera del server.

    Es el mismo criterio que ya aplicaba el panel de botones
    (`PlayerPanel.interaction_check`): sin esto, alguien desde otro canal podía
    saltar o parar la música de los demás.
    """
    async def predicado(ctx: commands.Context) -> bool:
        vc = ctx.voice_client
        if not vc or not vc.channel:
            return True     # no hay nada que proteger; el comando ya avisa
        voz = getattr(ctx.author, "voice", None)
        if voz and voz.channel == vc.channel:
            return True
        raise commands.CheckFailure("Tienes que estar en el mismo canal de voz que yo.")

    return commands.check(predicado)


def format_duration(milliseconds: int) -> str:
    if not milliseconds:
        return "?"
    seconds = milliseconds // 1000
    mins, secs = divmod(seconds, 60)
    return f"{mins}:{secs:02d}"

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._idle_tasks: dict[int, asyncio.Task] = {}
        self._panels: dict[int, tuple[discord.Message, PlayerPanel]] = {}
        self._session: aiohttp.ClientSession | None = None
        self._autoplay: dict[int, bool] = {}
        self._spotify_tasks: dict[int, asyncio.Task] = {}
        # (guild, identifier) -> intentos ya gastados en esa pista.
        self._reintentos: dict[tuple[int, str], int] = {}
        # guild -> temas seguidos que se perdieron después de gastar los reintentos.
        self._caidas: dict[int, int] = {}
        # Un arranque a la vez por servidor (ver `_arrancar_si_hace_falta`).
        self._arranques: dict[int, asyncio.Lock] = {}
        # Referencias fuertes a las tareas de fondo: el loop solo guarda
        # referencias débiles y una tarea sin dueño puede recolectarse a medias.
        self._tareas: set[asyncio.Task] = set()
        # Cómo viene la transmisión. Lo lee el panel por la API y lo dice el DJ.
        self.salud = Salud()
        self.dj = DJ()
        # La del bot, para que el player y el cog vean los mismos videos sin caso.
        # Los tests construyen el cog con `bot=None` y así cada uno tiene la suya.
        self.resolver = getattr(bot, "resolver", None) or resolver.Resolver()

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Nada de música por mensaje directo: sin servidor no hay canal de voz."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage(
                "Los comandos de música funcionan en un servidor, no por privado."
            )
        return True

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        feed_urls = DJ.load_feed_urls()
        if feed_urls:
            self.dj.feeds = FeedCache(feed_urls)
        if not tts.available():
            self.dj.enabled = False
            log.warning("espeak-ng no está instalado: el DJ queda desactivado")
        else:
            log.info("DJ activo (%s feeds)", len(feed_urls))

    async def cog_unload(self):
        for task in (*self._idle_tasks.values(), *self._spotify_tasks.values(), *self._tareas):
            task.cancel()
        for _, view in self._panels.values():
            view.stop()
        if self._session:
            await self._session.close()

    def _segundo_plano(self, coro) -> asyncio.Task:
        """Lanza una tarea y la retiene hasta que termine."""
        tarea = asyncio.create_task(coro)
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)
        return tarea

    # --- panel ---------------------------------------------------------------
    async def _delete_panel(self, guild_id: int):
        entry = self._panels.pop(guild_id, None)
        if not entry:
            return
        message, view = entry
        view.stop()
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    async def _post_panel(self, guild: discord.Guild, vc: wavelink.Player):
        await self._delete_panel(guild.id)
        view = PlayerPanel(self, guild.id)
        try:
            # Wavelink no ata el player a un canal de texto y hace falta un lugar donde publicar:
            # se guarda el último canal de texto en el propio vc.
            text_channel = getattr(vc, "text_channel", None)
            if text_channel:
                message = await text_channel.send(embed=build_embed(vc), view=view)
                self._panels[guild.id] = (message, view)
        except discord.HTTPException:
            log.warning("no pude postear el panel", exc_info=True)

    # --- ciclo de vida -------------------------------------------------------
    def _cancel_idle(self, guild_id: int):
        task = self._idle_tasks.pop(guild_id, None)
        if task:
            task.cancel()

    async def _schedule_idle_disconnect(self, guild: discord.Guild, vc: wavelink.Player):
        guild_id = guild.id
        self._cancel_idle(guild_id)

        async def _task():
            await asyncio.sleep(IDLE_TIMEOUT)
            if vc and vc.connected and not vc.playing:
                await vc.disconnect()
                self.dj.drop(guild_id)
                await self._delete_panel(guild_id)
                text_channel = getattr(vc, "text_channel", None)
                if text_channel:
                    await text_channel.send("Sin música por 5 min, me fui 👋")
            self._idle_tasks.pop(guild_id, None)

        self._idle_tasks[guild_id] = asyncio.create_task(_task())

    async def teardown(self, guild: discord.Guild, note: str | None = None):
        vc: wavelink.Player = guild.voice_client
        self._cancel_idle(guild.id)
        self._cancelar_spotify(guild.id)
        self.dj.drop(guild.id)
        for clave in [k for k in self._reintentos if k[0] == guild.id]:
            del self._reintentos[clave]
        self._caidas.pop(guild.id, None)
        self.salud.sin_siguiente(guild.id)
        if vc:
            # Los cortes del DJ que quedaron esperando en la cola no van a sonar
            # nunca: sin esto sus wavs se quedan en dj_cache hasta el próximo
            # arranque.
            for pista in vc.queue:
                self._borrar_clip_dj(pista)
            vc.queue.clear()
            vc.auto_queue.clear()
            await vc.disconnect()
        panel = self._panels.get(guild.id)
        await self._delete_panel(guild.id)
        if note and panel:
            await panel[0].channel.send(note)

    # --- reinicio del nodo de audio ---------------------------------------------
    async def recuperar_players(self, players) -> None:
        """Rehace los players cuando Lavalink arrancó una sesión nueva.

        Si el proceso de Lavalink se reinicia, wavelink reconecta con una sesión
        nueva (`resumed=False`) y los players del servidor ya no existen, pero
        los nuestros siguen con `_current` puesto: `playing` da verdadero, no
        llega ningún `track_end` y la cola no avanza nunca más. Se intenta
        retomar cada uno donde iba; si no se puede, se corta con un aviso.
        """
        for vc in players:
            if getattr(vc, "guild", None) is None:
                continue
            try:
                await self._retomar(vc)
            except Exception:  # noqa: BLE001 - un servidor roto no frena a los demás
                log.exception("No pude retomar el player de %s: lo corto", vc.guild)
                await self._cortar_por_nodo(vc)

    async def _retomar(self, vc: wavelink.Player) -> None:
        """Vuelve a mandarle la voz a la sesión nueva y repite el tema en curso."""
        # Es lo mismo que hace wavelink al cambiar de nodo (`switch_node`): el
        # session_id/token/endpoint de Discord siguen valiendo, solo hay que
        # contárselos a la sesión nueva. Si Lavalink los rechaza, el propio
        # método desconecta el player y `connected` queda en falso.
        await vc._dispatch_voice_update()
        if not vc.connected:
            raise RuntimeError("la sesión nueva rechazó el estado de voz")

        actual = vc.current
        if actual is None:
            log.info("Nodo reiniciado: voz restablecida en %s (sin tema en curso)", vc.guild)
            return

        # `_last_position` solo vale si Lavalink ya reportó ESTE tema (ver
        # `on_wavelink_track_exception`); si no, es la posición del anterior.
        posicion = 0
        if (getattr(vc, "_last_update", None) or 0) > getattr(vc, "_inicio_ns", 0):
            posicion = getattr(vc, "_last_position", 0) or 0
        # La cola queda intacta: solo se vuelve a tocar el tema que sonaba. Pasa
        # por `ResolvingPlayer.play`, que pide una URL fresca a yt-dlp (la
        # anterior pudo caducar). `add_history=False`: no es un tema nuevo.
        await vc.play(actual, start=posicion, paused=vc.paused, add_history=False)
        log.info(
            "Nodo reiniciado: retomé %r en %s desde %ss",
            resolver.titulo_de(actual), vc.guild, posicion // 1000,
        )

    async def _cortar_por_nodo(self, vc: wavelink.Player) -> None:
        """Último recurso cuando no se pudo retomar: se corta y se avisa."""
        canal = getattr(vc, "text_channel", None)
        con_panel = vc.guild.id in self._panels
        try:
            await self.teardown(vc.guild, note=AVISO_NODO_REINICIADO)
            # `teardown` solo avisa donde estaba el panel; sin panel, al canal
            # de texto del comando.
            if canal and not con_panel:
                await canal.send(AVISO_NODO_REINICIADO)
        except Exception:  # noqa: BLE001
            log.exception("No pude cortar limpio el player de %s", vc.guild)

    # --- autoplay y cola -----------------------------------------------------
    def autoplay_activo(self, guild_id: int) -> bool:
        return self._autoplay.get(guild_id, config.AUTOPLAY_DEFAULT)

    def _aplicar_autoplay(self, vc: wavelink.Player) -> None:
        """Traduce nuestro on/off al modo de wavelink.

        `enabled` es el que sale a buscar recomendaciones cuando se vacía la
        cola (arma una radio a partir del último tema). `partial` se limita a
        pasar al siguiente de NUESTRA cola. `disabled` no se usa nunca: con eso
        la cola directamente no avanza.
        """
        vc.autoplay = (
            wavelink.AutoPlayMode.enabled
            if self.autoplay_activo(vc.guild.id)
            else wavelink.AutoPlayMode.partial
        )

    def _espacio_en_cola(self, vc: wavelink.Player) -> int:
        return max(0, config.QUEUE_LIMIT - len(vc.queue))

    @staticmethod
    def _proximo(vc: wavelink.Player):
        """El tema que suena después: el de la cola, o el que puso el autoplay."""
        if vc.queue:
            return vc.queue[0]
        if vc.auto_queue:
            return vc.auto_queue[0]
        return None

    def _precargar_siguiente(self, vc: wavelink.Player) -> None:
        """Le pide al resolver que adelante la URL del tema que viene.

        Los clips del DJ no tienen URL que adelantar. Es una optimización: si
        falla, que no se lleve por delante el panel ni el corte del DJ que siguen
        al listener.
        """
        siguiente = self._proximo(vc)
        if siguiente is None or getattr(siguiente, "is_dj_clip", False):
            return
        # El del player, que es el que va a resolver después; en la práctica es el
        # mismo que `self.resolver` (`_connect_voice` los empareja).
        quien = getattr(vc, "resolver", None) or self.resolver
        try:
            quien.precargar(siguiente)
        except Exception:
            log.warning("No pude precargar la URL del tema que viene", exc_info=True)

    def _cancelar_spotify(self, guild_id: int) -> None:
        task = self._spotify_tasks.pop(guild_id, None)
        if task:
            task.cancel()

    # --- DJ ------------------------------------------------------------------
    async def _clip_a_track(self, clip) -> wavelink.Playable | None:
        """Convierte el .wav del DJ en un track que Lavalink pueda reproducir.

        `source=None` es obligatorio: por defecto `search()` le antepone
        `ytmsearch:` a lo que no sea una URL, y saldría a buscar la ruta del
        archivo en YouTube Music en vez de abrirlo con la fuente local.
        """
        try:
            resultados = await wavelink.Playable.search(str(clip.path), source=None)
        except Exception:
            log.warning("Lavalink no pudo cargar el clip del DJ", exc_info=True)
            return None
        if not resultados or isinstance(resultados, wavelink.Playlist):
            log.warning("Lavalink no encontró el clip del DJ en %s", clip.path)
            return None
        track = resultados[0]
        track.is_dj_clip = True
        return track

    async def _encolar_dj(self, vc: wavelink.Player, para: wavelink.Playable) -> None:
        """Sintetiza el corte del DJ y lo deja al frente, delante de `para`.

        Se mete en la cola en lugar de reproducirlo a mano porque el que va
        pasando de tema en tema es el AutoPlay de wavelink: si intentáramos
        colarlo en el momento del cambio, competiríamos con él.
        """
        await self.dj.prepare(vc.guild.id, para, self._session)
        clip = self.dj.take(vc.guild.id, para)
        if not clip:
            return
        # Mientras sintetizábamos pudo haber un skip, un shuffle o un !stop.
        if not vc.connected or self._proximo(vc) is not para:
            tts.cleanup(clip.path)
            return
        track = await self._clip_a_track(clip)
        if not track:
            tts.cleanup(clip.path)
            return
        # Siempre a `queue`, aunque el tema que presenta lo haya elegido el
        # autoplay: wavelink vacía primero `queue` y solo después va a
        # `auto_queue`, así que el clip queda justo antes de su canción.
        vc.queue.put_at(0, track)

    # --- wavelink events -----------------------------------------------------
    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        vc: wavelink.Player = payload.player
        # El wav del DJ ya sonó (o se cortó): no hace falta en disco. Si solo se
        # borrara al arrancar el bot, dj_cache crecería sin tope.
        if payload.track is not None:
            self._borrar_clip_dj(payload.track)
        if not vc or not vc.guild:
            return

        # Empieza a correr el hueco hasta la próxima pista, sea canción o clip.
        # Un skip también cuenta: es silencio que se escucha.
        self.salud.termino(vc.guild.id)

        # Terminó bien: el contador de reintentos ya no sirve. No se limpia al
        # arrancar: un tema que arranca y revienta al decodificar reiniciaba el
        # contador y se reintentaba sin fin.
        if payload.reason == "finished" and payload.track is not None:
            self._reintentos.pop((vc.guild.id, resolver.id_de_cancion(payload.track)), None)
            # Sonó una canción entera: la racha de temas perdidos se corta. El
            # clip del DJ no cuenta, que el aviso de "la salteo" también es uno.
            if not self._es_clip_dj(payload.track):
                self._caidas.pop(vc.guild.id, None)

        # Del "qué suena ahora" se encarga el AutoPlay de wavelink; aquí solo
        # atendemos el final de la cola. "replaced" es un skip: viene otro tema.
        # `auto_queue` también cuenta: con el autoplay encendido la transmisión
        # sigue aunque nuestra cola esté vacía, y ahí no hay que bajar el panel.
        if vc.queue or vc.auto_queue or payload.reason == "replaced":
            return

        # Nada más que sonar: lo que arranque después es un `!play` nuevo, no
        # una transición, y no tiene que medirse contra este final.
        self.salud.sin_siguiente(vc.guild.id)
        await self._delete_panel(vc.guild.id)
        self.dj.drop(vc.guild.id)
        await self._schedule_idle_disconnect(vc.guild, vc)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        """Lavalink no pudo sacar el audio: se reintenta antes de perder la pista.

        El fallo de YouTube es intermitente, así que reintentar sirve de verdad.
        La pista vuelve al frente de `queue` en vez de reproducirla a mano: el
        que va pasando de tema en tema es el AutoPlay, y wavelink vacía `queue`
        antes de ir a `auto_queue`, así que ahí la agarra sin pelearnos con él.
        """
        vc: wavelink.Player = payload.player
        if not vc or not vc.guild:
            return

        pista = payload.track
        # El payload viene deserializado de Lavalink y no trae los atributos que
        # les colgamos nosotros, así que al clip del DJ hay que reconocerlo por
        # la ruta. Un clip que falla no se reintenta: se vuelve a sintetizar solo
        # en el próximo corte.
        if pista is None or self._es_clip_dj(pista):
            return

        # wavelink lleva su propia cuenta de fallos y, al llegar a tres seguidos,
        # deja de avanzar la cola para siempre: después ya no la pone en cero ni
        # aunque suene algo. Un solo tema con sus dos reintentos llegaba a tres y
        # la transmisión quedaba muda hasta que el bot se iba del canal. Los
        # reintentos y el corte por fallos en cadena (`_caidas`) los lleva este
        # cog, así que esa cuenta se anula aquí. Tiene que ser antes del primer
        # await: esto corre antes que el `_auto_play_event` de este mismo final.
        vc._error_count = 0

        clave = (vc.guild.id, resolver.id_de_cancion(pista))
        motivo = self.resolver.motivo_definitivo(pista)
        self.salud.fallo(motivo)

        # Un fallo sin arreglo no gasta reintentos. Un tema bloqueado o borrado
        # no mejora porque se lo pida de nuevo: son treinta segundos de cola
        # congelada para algo que no va a sonar nunca.
        if motivo:
            self._reintentos.pop(clave, None)
            titulo = resolver.titulo_de(pista)
            log.info("Salteo %r: %s", titulo, motivo)
            canal = getattr(vc, "text_channel", None)
            if canal:
                await canal.send(f"**{titulo}** {motivo}, la salteo.")
            await self._decir(vc, self.salud.rendido(titulo, motivo))
            return

        gastados = self._reintentos.get(clave, 0)

        if gastados >= REINTENTOS_POR_PISTA:
            self._reintentos.pop(clave, None)
            caidas = self._caidas.get(vc.guild.id, 0) + 1
            self._caidas[vc.guild.id] = caidas
            titulo = resolver.titulo_de(pista)
            log.warning(
                "Sin audio para %r después de %s intentos: %s",
                titulo, gastados, payload.exception,
            )
            canal = getattr(vc, "text_channel", None)
            if caidas >= CAIDAS_SEGUIDAS_MAX:
                # Tema tras tema sin audio: YouTube no está entregando y seguir
                # probando solo quema la cola. Se deja la cuenta de wavelink en
                # su tope, antes de que mire este final, para que no pase al
                # siguiente; `!play` la retoma (ver `_arrancar_si_hace_falta`).
                vc._error_count = TOPE_DE_WAVELINK
                log.warning("%s temas seguidos sin audio: paro la cola", caidas)
                if canal:
                    await canal.send(
                        f"No pude sacar el audio de **{titulo}** ni de los "
                        f"{caidas - 1} anteriores: paro la cola. `!play` para retomar."
                    )
                return
            if canal:
                await canal.send(f"No pude sacar el audio de **{titulo}**, la salteo.")
            await self._decir(vc, self.salud.rendido(titulo))
            return

        self._reintentos[clave] = gastados + 1
        # Si se cortó a mitad de tema, el reintento sigue desde ahí (menos un
        # respiro de 2 s); el corte por 302 de googlevideo pasa en pleno stream.
        # `_last_position` solo vale si Lavalink ya reportó ESTE tema: si no, es
        # la posición del anterior y reanudar ahí revienta con EOFException.
        corte = 0
        # wavelink arranca `_last_update` en None hasta el primer playerUpdate.
        if (getattr(vc, "_last_update", None) or 0) > getattr(vc, "_inicio_ns", 0):
            corte = getattr(vc, "_last_position", 0) or 0
        largo = getattr(pista, "length", 0) or 0
        if hasattr(vc, "reanudar") and 5000 < corte and (not largo or corte < largo - 15000):
            vc.reanudar[clave[1]] = corte - 2000
        log.info(
            "Reintento %s/%s de %r: %s",
            gastados + 1, REINTENTOS_POR_PISTA, resolver.titulo_de(pista),
            payload.exception,
        )
        # Se reencola YA, sin esperar nada en el medio. wavelink decide qué hacer
        # apenas se muere el tema y lo primero que mira es si hay cola: si para
        # entonces todavía no la devolvimos, se va a buscar recomendaciones y, si
        # no encuentra, estaciona el player. La pista quedaría en una cola que ya
        # nadie va a mirar, que desde afuera es "el bot dejó de reproducir".
        #
        # El respiro que antes daba este sleep ahora lo da el propio resolver, que
        # se toma su viaje a yt-dlp antes de cada intento.
        if vc.connected:
            vc.queue.put_at(0, pista)

    async def _decir(self, vc: wavelink.Player, frase: str | None) -> None:
        """Que el DJ diga algo ahora mismo, al frente de la cola.

        Es el canal que sigue vivo justo cuando falla lo que hay que avisar, y
        como el tema se cayó el player está libre: suena de inmediato sin competir
        con nada. `frase` viene en None cuando toca callarse (ver Salud.rendido).
        """
        if not frase or not vc.connected:
            return
        clip = await self.dj.aviso(frase)
        if not clip:
            return
        track = await self._clip_a_track(clip)
        if track:
            vc.queue.put_at(0, track)
        else:
            tts.cleanup(clip.path)

    @staticmethod
    def _es_clip_dj(pista: wavelink.Playable) -> bool:
        """Si la pista es un wav del DJ y no una canción."""
        carpeta = str(tts.CACHE_DIR)
        return any(
            str(getattr(pista, campo, "") or "").startswith(carpeta)
            for campo in ("uri", "identifier")
        )

    def _borrar_clip_dj(self, pista: wavelink.Playable) -> None:
        """Borra el wav si la pista es un corte del DJ; una canción ni se toca."""
        if self._es_clip_dj(pista):
            tts.cleanup(getattr(pista, "uri", None) or pista.identifier)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        vc: wavelink.Player = payload.player
        if not vc or not vc.guild:
            return

        self._cancel_idle(vc.guild.id)
        vc._inicio_ns = time.monotonic_ns()

        # Mientras habla el DJ no se toca el panel: el que manda es el tema.
        # `payload.track` viene deserializado del evento de Lavalink y NO trae
        # los atributos que le colgamos nosotros; el que sí es `payload.original`.
        sonando = payload.original or payload.track
        es_clip = getattr(sonando, "is_dj_clip", False)

        # El hueco se mide con cualquier pista, también con el clip del DJ: el
        # silencio antes de un corte lo oye igual quien escucha. Por eso va
        # antes del `return` de los clips y aparte de `sono()`.
        hueco = self.salud.arranco(vc.guild.id)
        if hueco is not None:
            log.info(
                "Hueco de %.1f s hasta %r",
                hueco, "corte del DJ" if es_clip else resolver.titulo_de(sonando),
            )

        # La URL del tema que viene se adelanta con lo que suena ya en marcha, para
        # que el cambio no espere a yt-dlp. Va antes del `return` de los clips a
        # propósito: al arrancar la canción `_proximo` es la que sigue, pero el
        # corte del DJ entra a la cola después y se interpone; cuando el corte
        # arranca, `_proximo` ya es la que viene tras él, y el rato de la voz es
        # justo el que hace falta para resolverla. `precargar` deduplica.
        self._precargar_siguiente(vc)

        if es_clip:
            return

        # Que arranque una canción es la única prueba de que esto funciona. El corte
        # del DJ no cuenta: si contara, taparía para siempre que no suena música.
        self.salud.sono()

        await self._post_panel(vc.guild, vc)

        # El corte del próximo cambio se prepara ahora, con la canción sonando:
        # sintetizar la voz toma su tiempo y así llega listo.
        if self.dj.available():
            siguiente = self._proximo(vc)
            if siguiente is not None and not getattr(siguiente, "is_dj_clip", False):
                self._segundo_plano(self._encolar_dj(vc, siguiente))

    # --- helpers -------------------------------------------------------------
    async def _connect_voice(self, ctx: commands.Context) -> wavelink.Player | None:
        if not ctx.author.voice:
            await ctx.send("Entra a un canal de voz primero.")
            return None
        
        vc: wavelink.Player = ctx.voice_client
        if not vc:
            # ResolvingPlayer y no wavelink.Player: el audio de YouTube lo saca
            # yt-dlp justo antes de sonar (ver core/resolver.py).
            vc = await ctx.author.voice.channel.connect(cls=resolver.ResolvingPlayer)
            # Si el bot no tiene resolver, el player se armó con uno propio y el
            # cog no vería lo que ese anota: se les da el mismo a los dos.
            vc.resolver = self.resolver
        elif vc.channel != ctx.author.voice.channel:
            await vc.move_to(ctx.author.voice.channel)

        # Sin esto la cola no avanza: wavelink arranca con AutoPlayMode.disabled
        # y al terminar un tema se queda callado.
        self._aplicar_autoplay(vc)
        vc.text_channel = ctx.channel
        return vc

    # --- comandos ------------------------------------------------------------
    @staticmethod
    def _ocupado(vc: wavelink.Player) -> bool:
        """Si hay un tema sonando o a punto de sonar.

        `playing` solo no alcanza: da falso los segundos que un `play()` pasa
        esperando a yt-dlp, también cuando es el autoplay pasando de tema.
        """
        return vc.playing or bool(getattr(vc, "resolviendo", 0))

    async def _arrancar_si_hace_falta(self, vc: wavelink.Player) -> None:
        """Si no hay nada sonando, larga el primero de la cola.

        Va de a uno por servidor: dos `!play` seguidos (o uno que cae justo
        cuando el autoplay pasa de tema) veían los dos el player libre, sacaban
        cada uno un tema de la cola y el segundo pisaba al primero, que se
        perdía sin aviso.
        """
        async with self._arranques.setdefault(vc.guild.id, asyncio.Lock()):
            if self._ocupado(vc) or not vc.queue:
                return
            # Presentación del primer tema: aquí sí hay que esperar a que la voz
            # esté sintetizada, porque no hay nada sonando todavía.
            if self.dj.available():
                await self._encolar_dj(vc, vc.queue[0])
                # Mientras se sintetizaba pudo arrancar otro tema.
                if self._ocupado(vc) or not vc.queue:
                    return
            # Lo pidió alguien: si la cola se había parado por fallos en cadena,
            # esto la retoma.
            vc._error_count = 0
            self._caidas.pop(vc.guild.id, None)
            await vc.play(vc.queue.get())

    @commands.command(name='play', aliases=['p'])
    async def play(self, ctx: commands.Context, *, query: str):
        """Reproduce o encola una canción, una URL, una playlist o un link de Spotify."""
        vc = await self._connect_voice(ctx)
        if not vc:
            return

        self._cancel_idle(ctx.guild.id)

        if spotify.es_spotify(query):
            return await self._play_spotify(ctx, vc, query)

        espacio = self._espacio_en_cola(vc)
        if not espacio:
            return await ctx.send(
                f"La cola está llena ({config.QUEUE_LIMIT} temas). Saca algo con `!remove`."
            )

        async with ctx.typing():
            tracks: wavelink.Search = await wavelink.Playable.search(query)
            if not tracks:
                return await ctx.send("No encontré nada con eso.")

            if isinstance(tracks, wavelink.Playlist):
                # Las listas "mix"/"radio" de YouTube son infinitas por diseño,
                # así que la playlist se recorta antes de encolarla.
                temas = list(tracks)[:min(espacio, config.PLAYLIST_LIMIT)]
                for track in temas:
                    track.requester = ctx.author.display_name
                    await vc.queue.put_wait(track)
                aviso = f"📃 Encolé {len(temas)} temas de la playlist."
                if len(tracks) > len(temas):
                    aviso += f" (la lista trae {len(tracks)}, corté ahí)"
                await ctx.send(aviso)
            else:
                track = tracks[0]
                track.requester = ctx.author.display_name
                await vc.queue.put_wait(track)
                if self._ocupado(vc):
                    await ctx.send(f"Cola #{len(vc.queue)}: **{track.title}**")

            await self._arrancar_si_hace_falta(vc)

    @commands.command(name='playnext', aliases=['pn', 'next'])
    async def playnext(self, ctx: commands.Context, *, query: str):
        """Mete la canción al principio de la cola: es la próxima en sonar."""
        vc = await self._connect_voice(ctx)
        if not vc:
            return

        self._cancel_idle(ctx.guild.id)

        if not self._espacio_en_cola(vc):
            return await ctx.send(
                f"La cola está llena ({config.QUEUE_LIMIT} temas). Saca algo con `!remove`."
            )

        # De un link de Spotify se toma el primer tema: meter una lista entera
        # adelante de todo no es lo que nadie espera de "la próxima".
        if spotify.es_spotify(query):
            lista = await spotify.fetch(self._session, query, limit=1) if self._session else None
            if not lista:
                return await ctx.send("No pude leer ese link de Spotify.")
            query = lista.queries[0]

        async with ctx.typing():
            tracks: wavelink.Search = await wavelink.Playable.search(query)
        if not tracks:
            return await ctx.send("No encontré nada con eso.")
        if isinstance(tracks, wavelink.Playlist):
            return await ctx.send("Eso es una playlist entera: usa `!play` para encolarla.")

        track = tracks[0]
        track.requester = ctx.author.display_name

        if not self._ocupado(vc):
            await vc.queue.put_wait(track)
            return await self._arrancar_si_hace_falta(vc)

        # Al frente de todo, incluso de un corte del DJ que ya esté esperando:
        # el clip sigue pegado a la canción que presenta, que pasa a ser la
        # segunda.
        vc.queue.put_at(0, track)
        await ctx.send(f"🔝 La próxima: **{track.title}**")

    # --- Spotify --------------------------------------------------------------
    async def _buscar_uno(self, consulta: str, pedido_por: str) -> wavelink.Playable | None:
        """Un nombre de Spotify → el tema en YouTube. None si no aparece."""
        try:
            tracks: wavelink.Search = await wavelink.Playable.search(consulta)
        except Exception:
            log.warning("falló la búsqueda de %r", consulta, exc_info=True)
            return None
        if not tracks or isinstance(tracks, wavelink.Playlist):
            return None
        track = tracks[0]
        track.requester = pedido_por
        return track

    async def _play_spotify(self, ctx: commands.Context, vc: wavelink.Player, url: str):
        """Encola un link de Spotify buscando cada tema por nombre en YouTube."""
        if not self._session:
            return await ctx.send("No tengo con qué salir a internet ahora mismo.")

        tope = min(self._espacio_en_cola(vc), config.PLAYLIST_LIMIT)
        if not tope:
            return await ctx.send(
                f"La cola está llena ({config.QUEUE_LIMIT} temas). Saca algo con `!remove`."
            )

        async with ctx.typing():
            lista = await spotify.fetch(self._session, url, limit=tope)
        if not lista:
            return await ctx.send(
                "No pude leer ese link de Spotify. ¿Es público? "
                "(las listas privadas o de tu biblioteca no se ven desde afuera)"
            )

        pedido_por = ctx.author.display_name
        primero = await self._buscar_uno(lista.queries[0], pedido_por)
        if primero:
            await vc.queue.put_wait(primero)

        restantes = lista.queries[1:]
        if len(lista.queries) == 1:
            await ctx.send(
                f"🎧 Spotify — **{primero.title}**" if primero
                else f"No encontré **{lista.queries[0]}** en YouTube."
            )
        else:
            aviso = f"🎧 Spotify — **{lista.name}**: buscando {len(lista.queries)} temas en YouTube…"
            if lista.truncada:
                aviso += f"\n(el link público de Spotify entrega hasta {len(lista.queries)}; el resto no lo veo)"
            await ctx.send(aviso)

        # Arrancar YA con el primero: el resto se sigue buscando de fondo, que
        # para 50 temas son varios segundos y nadie quiere esperarlos callado.
        await self._arrancar_si_hace_falta(vc)

        if restantes:
            self._cancelar_spotify(ctx.guild.id)
            self._spotify_tasks[ctx.guild.id] = asyncio.create_task(
                self._encolar_spotify(ctx, vc, restantes, pedido_por)
            )

    async def _encolar_spotify(
        self,
        ctx: commands.Context,
        vc: wavelink.Player,
        consultas: list[str],
        pedido_por: str,
    ) -> None:
        encolados, perdidos = 0, 0
        try:
            for i in range(0, len(consultas), SPOTIFY_LOTE):
                if not vc.connected:
                    return
                lote = consultas[i:i + SPOTIFY_LOTE]
                # En lote pero manteniendo el orden del disco/playlist.
                resultados = await asyncio.gather(
                    *(self._buscar_uno(c, pedido_por) for c in lote)
                )
                for track in resultados:
                    if track is None:
                        perdidos += 1
                    elif self._espacio_en_cola(vc):
                        await vc.queue.put_wait(track)
                        encolados += 1
                await self._arrancar_si_hace_falta(vc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("se cayó la carga de la lista de Spotify")
        finally:
            self._spotify_tasks.pop(ctx.guild.id, None)

        resumen = f"🎧 Listo: {encolados} temas más en la cola."
        if perdidos:
            resumen += f" No encontré {perdidos} en YouTube."
        try:
            await ctx.send(resumen)
        except discord.HTTPException:
            log.debug("no pude avisar el fin de la carga de Spotify", exc_info=True)

    @commands.command(name='autoplay', aliases=['ap'])
    async def autoplay_cmd(self, ctx: commands.Context, modo: str = ""):
        """Cuando se acaba la cola, sigue solo con temas parecidos. `!autoplay on|off`."""
        modo = modo.lower()
        if modo not in ("on", "off"):
            estado = "encendido" if self.autoplay_activo(ctx.guild.id) else "apagado"
            return await ctx.send(
                f"El autoplay está **{estado}**. Usa `!autoplay on|off`.\n"
                "Con esto encendido, al vaciarse la cola sigo con temas parecidos "
                "al último en vez de callarme."
            )

        self._autoplay[ctx.guild.id] = modo == "on"
        vc: wavelink.Player = ctx.voice_client
        if vc:
            self._aplicar_autoplay(vc)
            if modo == "off":
                # Lo ya recomendado se descarta: si no, sigue sonando un rato
                # más y parece que el comando no hizo nada.
                vc.auto_queue.clear()
        await ctx.send(
            "🔮 Autoplay encendido: cuando se acabe la cola sigo solo."
            if modo == "on"
            else "Autoplay apagado: al final de la cola me quedo callado."
        )

    @commands.command(name='search', aliases=['buscar'])
    async def search_cmd(self, ctx: commands.Context, *, query: str):
        """Busca y muestra 5 opciones para elegir con una reacción."""
        vc = await self._connect_voice(ctx)
        if not vc:
            return

        async with ctx.typing():
            tracks: wavelink.Search = await wavelink.Playable.search(query)
        
        if not tracks or isinstance(tracks, wavelink.Playlist):
            return await ctx.send("No encontré resultados individuales.")
        
        options = tracks[:5]
        numbers = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"][:len(options)]
        lines = [
            f"{numbers[i]} {opt.title} [{format_duration(opt.length)}]"
            for i, opt in enumerate(options)
        ]
        prompt = await ctx.send("\n".join(lines) + "\n\nReacciona con el número para elegir (30s).")
        for emoji in numbers:
            await prompt.add_reaction(emoji)

        def check(reaction: discord.Reaction, user: discord.Member):
            return (
                user == ctx.author
                and reaction.message.id == prompt.id
                and str(reaction.emoji) in numbers
            )

        try:
            reaction, _ = await self.bot.wait_for('reaction_add', timeout=30, check=check)
        except asyncio.TimeoutError:
            return await ctx.send("Se acabó el tiempo, no elegiste nada.")

        chosen = options[numbers.index(str(reaction.emoji))]
        chosen.requester = ctx.author.display_name
        
        await vc.queue.put_wait(chosen)
        if self._ocupado(vc):
            await ctx.send(f"Cola #{len(vc.queue)}: **{chosen.title}**")
        else:
            await self._arrancar_si_hace_falta(vc)

    @en_el_mismo_canal()
    @commands.command(name='pause')
    async def pause(self, ctx: commands.Context):
        """Pausa lo que esté sonando."""
        msg = await controller.pause(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.command(name='resume', aliases=['r'])
    async def resume(self, ctx: commands.Context):
        """Reanuda lo que estaba pausado."""
        msg = await controller.resume(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.command(name='skip', aliases=['s'])
    async def skip(self, ctx: commands.Context):
        """Salta a la canción siguiente."""
        msg = await controller.skip(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.command(name='stop')
    async def stop(self, ctx: commands.Context):
        """Para todo, vacía la cola y me salgo del canal."""
        if not ctx.voice_client:
            return await ctx.send("No estoy en ningún canal.")
        await self.teardown(ctx.guild)
        await ctx.send("Parado 🛑")

    @commands.command(name='queue', aliases=['q'])
    async def show_queue(self, ctx: commands.Context):
        """Muestra la cola de reproducción."""
        vc: wavelink.Player = ctx.voice_client
        if not vc or (not vc.playing and not vc.queue):
            return await ctx.send("La cola está vacía.")

        lines = []
        if vc.current:
            tag = " 🔁" if vc.queue.mode == wavelink.QueueMode.loop else ""
            lines.append(f"🎵 **Ahora:** {vc.current.title}{tag}")
        for i, t in enumerate(vc.queue, 1):
            lines.append(f"`{i}.` {t.title} [{format_duration(t.length)}]")
            if i >= 20:
                lines.append(f"... y {len(vc.queue) - 20} más.")
                break

        if self.autoplay_activo(ctx.guild.id):
            if vc.auto_queue:
                lines.append(f"\n🔮 **Autoplay:** sigue con *{vc.auto_queue[0].title}*")
            elif not vc.queue:
                lines.append("\n🔮 **Autoplay** encendido: al terminar esta busco temas parecidos.")

        for chunk in split_message("\n".join(lines)):
            await ctx.send(chunk)

    @commands.command(name='nowplaying', aliases=['np'])
    async def nowplaying(self, ctx: commands.Context):
        """Dice qué canción está sonando."""
        vc: wavelink.Player = ctx.voice_client
        if not vc or not vc.current:
            return await ctx.send("No hay nada reproduciendo.")
        t = vc.current
        tag = " 🔁" if vc.queue.mode == wavelink.QueueMode.loop else ""
        req = getattr(t, "requester", "?")
        await ctx.send(
            f"🎵 **{t.title}**{tag}\n"
            f"Duración: `{format_duration(t.length)}` — Pedido por: {req}"
        )

    @commands.command(name='panel')
    async def panel_cmd(self, ctx: commands.Context):
        """Vuelve a postear el panel de botones."""
        vc: wavelink.Player = ctx.voice_client
        if not vc or not vc.current:
            return await ctx.send("No hay nada reproduciendo.")
        await self._post_panel(ctx.guild, vc)

    @en_el_mismo_canal()
    @commands.command(name='loop', aliases=['l'])
    async def loop_cmd(self, ctx: commands.Context):
        """Enciende o apaga la repetición de la canción actual."""
        msg = await controller.toggle_loop(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.command(name='shuffle')
    async def shuffle(self, ctx: commands.Context):
        """Mezcla el orden de la cola."""
        msg = controller.shuffle(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.command(name='volume', aliases=['vol'])
    async def volume(self, ctx: commands.Context, vol: int):
        """Ajusta el volumen, de 0 a 100."""
        msg = await controller.set_volume(ctx.voice_client, vol)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.command(name='remove', aliases=['rm'])
    async def remove(self, ctx: commands.Context, pos: int):
        """Saca de la cola la canción en esa posición."""
        vc: wavelink.Player = ctx.voice_client
        if not vc or not vc.queue:
            return await ctx.send("La cola está vacía.")
        try:
            track = vc.queue[pos - 1]
            del vc.queue[pos - 1]
            await ctx.send(f"Eliminado: **{track.title}**")
        except IndexError:
            await ctx.send(f"No hay canción en la posición {pos}.")

    @commands.command(name='dj')
    async def dj_cmd(self, ctx: commands.Context, *args: str):
        """El DJ de radio: `!dj` habla en el próximo corte, `!dj on|off` lo enciende o apaga, `!dj vol 0-200` ajusta su voz."""
        if not args:
            if not self.dj.available():
                return await ctx.send("El DJ está apagado o falta espeak-ng.")
            self.dj.force(ctx.guild.id)
            return await ctx.send("🎙️ El DJ habla en el próximo corte.")

        opcion = args[0].lower()
        if opcion in ("on", "off"):
            if opcion == "on" and not tts.available():
                return await ctx.send("No puedo: falta `espeak-ng` en el sistema.")
            self.dj.enabled = opcion == "on"
            return await ctx.send(f"DJ {'activado 🎙️' if self.dj.enabled else 'apagado'}")

        if opcion in ("vol", "volumen"):
            if len(args) < 2:
                return await ctx.send(f"La voz del DJ está al **{self.dj.volume}%**. Usa `!dj vol 0-200`.")
            try:
                pedido = int(args[1])
            except ValueError:
                return await ctx.send("El volumen del DJ va entre 0 y 200.")
            aplicado = self.dj.set_volume(pedido)
            if aplicado == 0:
                return await ctx.send("DJ muteado 🔇 (`!dj vol 100` lo devuelve).")
            # El clip se sintetiza mientras suena el tema anterior, así que el
            # que ya está listo sale con el volumen viejo.
            return await ctx.send(f"Voz del DJ al **{aplicado}%** 🎚️ (se escucha desde el próximo corte).")

        await ctx.send("Uso: `!dj`, `!dj on|off`, `!dj vol 0-200`")

    @commands.command(name='lyrics', aliases=['ly'])
    async def lyrics_cmd(self, ctx: commands.Context, *, query: str = ""):
        """Busca la letra de la canción actual (o la que le pidas)."""
        vc: wavelink.Player = ctx.voice_client
        if not vc or not vc.current and not query:
            return await ctx.send("No hay nada reproduciendo.")

        if not self._session:
            return await ctx.send("El buscador de letras no está listo.")

        status_msg = await ctx.send("Buscando letra...")
        original_title = query or vc.current.title
        title = lyrics.clean_song_title(original_title)
        artist, song = lyrics.split_artist_song(original_title)

        try:
            text = await asyncio.wait_for(
                lyrics.find(self._session, original_title, artist, song),
                timeout=lyrics.TOTAL_TIMEOUT,
            )
            if text:
                return await status_msg.edit(
                    content=f"**{title}**\n```\n{text[:lyrics.MAX_CHARS]}\n```"
                )
            await status_msg.edit(content="No encontré la letra. Prueba con `!lyrics artista - canción`.")
        except asyncio.TimeoutError:
            await status_msg.edit(content="La búsqueda tardó demasiado.")
        except Exception:
            log.exception("Error buscando la letra")
            await status_msg.edit(content="Error buscando la letra.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
