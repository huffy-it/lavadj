import asyncio
import logging
import time
from functools import partial

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import wavelink

from core import cola_spotify, config, controller, lyrics, persistencia, resolver, spotify, tts
from core.dj import DJ
from core.estacion import Estacion
from core.feeds import FeedCache
from core.locutor import Locutor, proximo
from core.messages import split_message
from core.panel import PlayerPanel
from core.reintentos import REINTENTOS_POR_PISTA, Accion, decidir
from core.salud import Salud
from core.sponsorblock import Segmento, llega_al_final

log = logging.getLogger("music")

IDLE_TIMEOUT = config.IDLE_TIMEOUT

# El tope fijo de wavelink: con su cuenta de fallos ahí, deja de avanzar la cola.
TOPE_DE_WAVELINK = 3

# Lo que se avisa cuando el nodo de audio se reinició y no hubo manera de retomar.
AVISO_NODO_REINICIADO = (
    "El nodo de audio se reinició y no pude retomar la transmisión: "
    "la corté. Usa `!play` para volver a empezar."
)

# Se actúa a la segunda revisión atascada: un mudo dura entre 20 y 40 s.
VIGILAR_CADA = 20

# Cada cuánto se fotografían las colas: es lo máximo que se pierde si el bot se cae de golpe.
GUARDAR_CADA = 15
# Aunque nada cambie, se reescribe cada tanto para que la posición y la hora no queden viejas.
REFRESCAR_CADA = 60

AVISO_RETOMADA = "Volví: sigo con la cola."

# Cada cuánto, como mucho, mira un vigía de segmento por dónde va el tema: así nota una pausa o un seek del usuario.
REVISAR_SEGMENTO = 2.0
PISO_DE_REVISION = 0.25
# Una URL de googlevideo usada minutos después de pedida suele fallar al instante (302): se pide casi al final del tema.
PRECARGAR_ANTES = 30
# Como mucho cada tanto mira la tarea de precarga por dónde va el tema: así nota una pausa o un seek del usuario.
REVISAR_PRECARGA = 5.0
# Lavalink reporta la posición cada 5 s; con un reporte más viejo (tras una pausa larga) no se extrapola.
REPORTE_VIGENTE_MS = 6000

# Autocompletado de /play y /playnext. Discord da 3 s para contestar y recorta a 100 caracteres y 25 opciones.
SUGERENCIAS_MIN = 3
SUGERENCIAS_MAX = 25
SUGERENCIAS_ESPERA = 2.5
SUGERENCIAS_VIGENCIA = 300
SUGERENCIAS_EN_CACHE = 64
LARGO_DISCORD = 100


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


def es_url(texto: str) -> bool:
    return texto.lower().startswith(("http://", "https://"))


def opcion_de(pista: wavelink.Playable) -> app_commands.Choice[str] | None:
    """Una sugerencia "Artista – Título (3:45)"; vale la URL del tema si cabe en el límite de Discord, si no el título."""
    titulo = (pista.title or "").strip()
    if not titulo:
        return None
    autor = (pista.author or "").strip()
    duracion = " (en vivo)" if pista.is_stream else f" ({format_duration(pista.length)})" if pista.length else ""
    nombre = f"{autor} – {titulo}" if autor else titulo
    if len(nombre) + len(duracion) > LARGO_DISCORD:
        nombre = nombre[:LARGO_DISCORD - len(duracion) - 1] + "…"
    uri = pista.uri or ""
    return app_commands.Choice(name=nombre + duracion, value=uri if uri and len(uri) <= LARGO_DISCORD else titulo[:LARGO_DISCORD])


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self._estaciones: dict[int, Estacion] = {}
        # Una preferencia del servidor, no estado de la transmisión: sobrevive a `teardown`.
        self._autoplay: dict[int, bool] = {}
        # Lo ya buscado para el autocompletado: texto -> (vence, opciones). Mientras se teclea se repiten las consultas.
        self._sugerencias: dict[str, tuple[float, list[app_commands.Choice[str]]]] = {}
        # Referencias fuertes a las tareas de fondo: el loop solo guarda
        # referencias débiles y una tarea sin dueño puede recolectarse a medias.
        self._tareas: set[asyncio.Task] = set()
        self._vigilante: asyncio.Task | None = None
        self._guardador: asyncio.Task | None = None
        # La vista persistente del panel (la registrada en el bot): atiende a los paneles que quedaron de antes de un reinicio.
        self._vista: PlayerPanel | None = None
        # De lo último escrito en disco, para no reescribirlo si nada cambió.
        self._huella: str | None = None
        self._escrita = 0.0
        # esperando -> retomando -> activa -> cerrada. Solo en `activa` se escribe: antes, una
        # foto vacía pisaría las guardadas que aún no se leyeron; después de cerrar, también.
        self._persistencia = "esperando"
        # Cómo viene la transmisión. Lo lee el panel por la API y lo dice el DJ.
        self.salud = Salud()
        self.dj = DJ()
        self.locutor = Locutor(self.dj)
        # Una para todo el bot: la letra en vivo de Discord y `/api/letra` del panel web no buscan dos veces la misma.
        self.letras = lyrics.Letras()
        # La del bot, para que el player y el cog vean los mismos videos sin caso.
        # Los tests construyen el cog con `bot=None` y así cada uno tiene la suya.
        self.resolver = getattr(bot, "resolver", None) or resolver.Resolver()

    def estacion(self, guild_id: int) -> Estacion:
        """El estado del servidor; se crea la primera vez que se pide."""
        est = self._estaciones.get(guild_id)
        if est is None:
            est = self._estaciones[guild_id] = Estacion()
        return est

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
        self._vigilante = self._segundo_plano(self._vigilar())
        self._guardador = self._segundo_plano(self._guardar_en_bucle())
        if self.bot is not None:    # los tests construyen el cog sin bot
            self._vista = PlayerPanel(self)
            self.bot.add_view(self._vista)

    async def cog_unload(self):
        for est in self._estaciones.values():
            est.cancelar_tareas()
        for task in self._tareas:
            task.cancel()
        await self._borrar_mensajes()
        if self._vista:
            self._vista.stop()
        if self._session:
            await self._session.close()
        await self.resolver.cerrar()

    def _segundo_plano(self, coro) -> asyncio.Task:
        """Lanza una tarea y la retiene hasta que termine."""
        tarea = asyncio.create_task(coro)
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)
        return tarea

    # --- panel ---------------------------------------------------------------
    async def _delete_panel(self, guild_id: int):
        est = self.estacion(guild_id)
        entry, est.panel = est.panel, None
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
        view = PlayerPanel(self)
        try:
            # Wavelink no ata el player a un canal de texto y hace falta un lugar donde publicar:
            # se guarda el último canal de texto en el propio vc.
            text_channel = getattr(vc, "text_channel", None)
            if text_channel:
                message = await text_channel.send(embed=view.pintar(vc, guild), view=view)
                self.estacion(guild.id).panel = (message, view)
        except discord.HTTPException:
            log.warning("no pude postear el panel", exc_info=True)

    async def _borrar_mensajes(self) -> None:
        """Borra el panel y la letra en vivo de cada servidor: sin el bot, sus botones quedarían muertos y la letra sin editar."""
        for guild_id, est in list(self._estaciones.items()):
            try:
                await self._delete_panel(guild_id)
                await est.letra.parar()
            except Exception:  # noqa: BLE001 - cerrar tiene que seguir pase lo que pase
                log.exception("No pude borrar los mensajes de %s al cerrar", guild_id)

    # --- ciclo de vida -------------------------------------------------------
    async def _schedule_idle_disconnect(self, guild: discord.Guild, vc: wavelink.Player):
        guild_id = guild.id
        est = self.estacion(guild_id)
        est.cancelar_idle()

        async def _task():
            await asyncio.sleep(IDLE_TIMEOUT)
            if vc and vc.connected and not vc.playing:
                await vc.disconnect()
                self.dj.drop(guild_id)
                await self._delete_panel(guild_id)
                text_channel = getattr(vc, "text_channel", None)
                if text_channel:
                    await text_channel.send("Sin música por 5 min, me fui 👋")
            est.idle = None

        est.idle = asyncio.create_task(_task())

    async def teardown(self, guild: discord.Guild, note: str | None = None):
        vc: wavelink.Player = guild.voice_client
        est = self.estacion(guild.id)
        est.cancelar_tareas()
        self.dj.drop(guild.id)
        self.salud.sin_siguiente(guild.id)
        if vc:
            # Los cortes del DJ que esperan en la cola no van a sonar nunca: sin
            # esto sus wavs se quedan en dj_cache hasta el próximo arranque.
            for pista in vc.queue:
                self.locutor.borrar_clip(pista)
            vc.queue.clear()
            vc.auto_queue.clear()
            await vc.disconnect()
        # Después de desconectar: mientras tanto el guardador aún pudo fotografiar el player.
        persistencia.borrar(guild.id)
        panel = est.panel
        await self._delete_panel(guild.id)
        await est.letra.apagar()
        self._estaciones.pop(guild.id, None)
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

        posicion = self._posicion(vc)
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
        con_panel = self.estacion(vc.guild.id).panel is not None
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

    def poner_autoplay(self, guild_id: int, vc: wavelink.Player | None, activo: bool) -> str:
        """Enciende o apaga el autoplay del servidor, lo aplica al player si lo hay y devuelve el aviso."""
        self._autoplay[guild_id] = activo
        if vc:
            self._aplicar_autoplay(vc)
            if not activo:
                # Lo ya recomendado se descarta: si no, sigue sonando un rato
                # más y parece que el comando no hizo nada.
                vc.auto_queue.clear()
        return (
            "🔮 Autoplay encendido: cuando se acabe la cola sigo solo."
            if activo
            else "Autoplay apagado: al final de la cola me quedo callado."
        )

    def _espacio_en_cola(self, vc: wavelink.Player) -> int:
        return max(0, config.QUEUE_LIMIT - len(vc.queue))

    def _precargar_siguiente(self, vc: wavelink.Player) -> None:
        """Le pide al resolver que adelante la URL del tema que viene.

        Los clips del DJ no tienen URL que adelantar. Es una optimización: si
        falla, que no se lleve por delante el panel ni el corte del DJ que siguen
        al listener.
        """
        siguiente = proximo(vc)
        if siguiente is None or getattr(siguiente, "is_dj_clip", False):
            return
        # El del player, que es el que va a resolver después; en la práctica es el
        # mismo que `self.resolver` (`_connect_voice` los empareja).
        quien = getattr(vc, "resolver", None) or self.resolver
        try:
            quien.precargar(siguiente)
        except Exception:
            log.warning("No pude precargar la URL del tema que viene", exc_info=True)

    def _final_de(self, vc: wavelink.Player, pista: wavelink.Playable) -> int:
        """Ms donde acaba `pista` para el cog: su largo, o donde empieza la parte final que no es música; 0 si no se sabe."""
        largo = 0 if getattr(pista, "is_stream", False) else int(getattr(pista, "length", 0) or 0)
        quien = getattr(vc, "resolver", None) or self.resolver
        try:
            partes = quien.segmentos_de(pista) if largo else ()
        except Exception:  # noqa: BLE001 - sin las marcas vale el largo
            log.debug("No pude leer las partes que no son música del tema", exc_info=True)
            partes = ()
        return min((parte.inicio for parte in partes if llega_al_final(parte, largo)), default=largo)

    async def _precargar_al_final(self, vc: wavelink.Player, est: Estacion, pista: wavelink.Playable) -> None:
        """Espera a que a `pista` le queden `PRECARGAR_ANTES` s y entonces adelanta la URL de lo que venga.

        Mira la posición real cada `REVISAR_PRECARGA` como mucho: una pausa o un seek del usuario mueven
        la cuenta y así se notan. Lo que viene se elige al final, porque la cola pudo cambiar.
        """
        yo = asyncio.current_task()
        try:
            while True:
                if vc.current is not pista or not vc.connected:
                    return
                falta = (self._final_de(vc, pista) - self.posicion_viva(vc)) / 1000 - PRECARGAR_ANTES
                if falta <= 0:
                    break
                await asyncio.sleep(max(PISO_DE_REVISION, min(falta, REVISAR_PRECARGA)))
            self._precargar_siguiente(vc)
        except Exception:  # noqa: BLE001 - tarea suelta: nada de lo que pase puede escapar
            log.warning("No pude precargar a tiempo la URL del tema que viene", exc_info=True)
        finally:
            if est.precarga is yo:
                est.precarga = None

    @staticmethod
    def _posicion(vc: wavelink.Player) -> int:
        """Ms por donde iba el tema en curso; 0 si Lavalink aún no lo reportó.

        Sin ese reporte `_last_position` es la del tema anterior y reanudar ahí
        revienta con EOFException. wavelink deja `_last_update` en None hasta el
        primer playerUpdate.
        """
        if (getattr(vc, "_last_update", None) or 0) > getattr(vc, "_inicio_ns", 0):
            return getattr(vc, "_last_position", 0) or 0
        return 0

    @staticmethod
    def posicion_viva(vc: wavelink.Player) -> int:
        """Ms por donde va el tema ahora, sin adelantarse nunca a lo que Lavalink contó.

        Entre reportes suma el tiempo corrido, pero no si está en pausa ni con un reporte viejo:
        tras una pausa larga extrapolar adelantaría la cuenta y se saltaría música.
        """
        reporte = getattr(vc, "_last_update", None) or 0
        arranque = getattr(vc, "_inicio_ns", 0)
        if reporte > arranque:
            base, desde = getattr(vc, "_last_position", 0) or 0, reporte
        else:
            base, desde = getattr(vc, "arranque_ms", 0), arranque
        corrido = (time.monotonic_ns() - desde) // 1_000_000
        return base if vc.paused or corrido > REPORTE_VIGENTE_MS else base + corrido

    def _programar_saltos(self, vc: wavelink.Player, sonando: wavelink.Playable) -> None:
        """Un vigía por cada parte que no es música de la canción que arranca."""
        est = self.estacion(vc.guild.id)
        est.cancelar_segmentos()
        if not resolver.video_de(sonando):
            return
        quien = getattr(vc, "resolver", None) or self.resolver
        try:
            segmentos = quien.segmentos_de(sonando)
        except Exception:  # noqa: BLE001 - es un extra: que no se lleve por delante el panel ni el corte del DJ
            log.warning("No pude leer las partes que no son música del tema", exc_info=True)
            return
        est.segmentos.extend(
            asyncio.create_task(self._vigilar_segmento(vc, est, sonando, segmento)) for segmento in segmentos
        )

    async def _vigilar_segmento(
        self, vc: wavelink.Player, est: Estacion, pista: wavelink.Playable, segmento: Segmento
    ) -> None:
        """Espera a que `pista` llegue al segmento y lo salta: `seek` a su final, o el tema siguiente si llega al final.

        Mira la posición real cada `REVISAR_SEGMENTO` como mucho en vez de dormir hasta el inicio: una pausa
        o un seek del usuario mueven la cuenta y así se notan sin depender de un evento, que wavelink no emite.
        """
        yo = asyncio.current_task()
        try:
            while True:
                posicion = self.posicion_viva(vc)
                if vc.current is not pista or not vc.connected or posicion >= segmento.fin:
                    return
                falta = (segmento.inicio - posicion) / 1000
                if falta <= 0:
                    break
                await asyncio.sleep(max(PISO_DE_REVISION, min(falta, REVISAR_SEGMENTO)))
            # Sale de la lista antes de actuar: el `track_end` que provoca el skip no tiene que cancelarla a medias.
            if yo in est.segmentos:
                est.segmentos.remove(yo)
            titulo = resolver.titulo_de(pista)
            if llega_al_final(segmento, getattr(pista, "length", 0) or 0):
                log.info("Salteo %r: del segundo %.0f al final no es música", titulo, segmento.inicio / 1000)
                await controller.skip(vc)
            else:
                log.info(
                    "Salto la parte que no es música de %r: del segundo %.0f al %.0f",
                    titulo, segmento.inicio / 1000, segmento.fin / 1000,
                )
                await vc.seek(segmento.fin)
        except Exception:  # noqa: BLE001 - tarea suelta: nada de lo que pase puede escapar
            log.warning("No pude saltar la parte que no es música de %r", resolver.titulo_de(pista), exc_info=True)
        finally:
            if yo in est.segmentos:
                est.segmentos.remove(yo)

    # --- wavelink events -----------------------------------------------------
    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        vc: wavelink.Player = payload.player
        # El wav del DJ ya sonó (o se cortó): no hace falta en disco. Si solo se
        # borrara al arrancar el bot, dj_cache crecería sin tope.
        if payload.track is not None:
            self.locutor.borrar_clip(payload.track)
        if not vc or not vc.guild:
            return

        # Los vigías de segmentos y la precarga son de esta canción: la que siga programa los suyos.
        if est := self._estaciones.get(vc.guild.id):
            est.cancelar_segmentos()
            est.cancelar_precarga()

        # Empieza a correr el hueco hasta la próxima pista, sea canción o clip.
        # Un skip también cuenta: es silencio que se escucha.
        self.salud.termino(vc.guild.id)

        # Terminó bien: el contador de reintentos ya no sirve. No se limpia al
        # arrancar: un tema que arranca y revienta al decodificar reiniciaba el
        # contador y se reintentaba sin fin.
        if payload.reason == "finished" and payload.track is not None:
            est = self.estacion(vc.guild.id)
            est.reintentos.pop(resolver.id_de_cancion(payload.track), None)
            # Sonó una canción entera: la racha de temas perdidos se corta. El
            # clip del DJ no cuenta, que el aviso de "la salteo" también es uno.
            if not self.locutor.es_clip(payload.track):
                est.caidas = 0

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
        await self.estacion(vc.guild.id).letra.apagar()
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
        # El payload no trae los atributos que le colgamos nosotros: al clip del DJ
        # hay que reconocerlo por la ruta. Un clip que falla no se reintenta: se
        # vuelve a sintetizar solo en el próximo corte.
        if pista is None or self.locutor.es_clip(pista):
            return

        # wavelink cuenta fallos y, a los tres seguidos, deja de avanzar la cola
        # para siempre. Los reintentos y las caídas los lleva este cog, así que esa
        # cuenta se anula. Tiene que ser antes del primer await: esto corre antes
        # que el `_auto_play_event` de este mismo final.
        vc._error_count = 0

        est = self.estacion(vc.guild.id)
        cancion = resolver.id_de_cancion(pista)
        motivo = self.resolver.motivo_definitivo(pista)
        self.salud.fallo(motivo)
        gastados = est.reintentos.get(cancion, 0)
        decision = decidir(
            motivo, gastados, est.caidas, self._posicion(vc), getattr(pista, "length", 0) or 0
        )
        titulo = resolver.titulo_de(pista)
        canal = getattr(vc, "text_channel", None)

        if decision.accion is Accion.SALTAR:
            est.reintentos.pop(cancion, None)
            log.info("Salteo %r: %s", titulo, motivo)
            if canal:
                await canal.send(f"**{titulo}** {motivo}, la salteo.")
            await self.locutor.decir(vc, self.salud.rendido(titulo, motivo))
        elif decision.accion is Accion.REINTENTAR:
            est.reintentos[cancion] = gastados + 1
            # Si se cortó a mitad de tema, el reintento sigue desde ahí; el corte
            # por 302 de googlevideo pasa en pleno stream.
            if decision.reanudar_en is not None and hasattr(vc, "reanudar"):
                vc.reanudar[cancion] = decision.reanudar_en
            log.info(
                "Reintento %s/%s de %r: %s",
                gastados + 1, REINTENTOS_POR_PISTA, titulo, payload.exception,
            )
            # Se reencola YA, sin esperar nada: wavelink mira la cola apenas muere el
            # tema y, si está vacía, busca recomendaciones y estaciona el player (desde
            # afuera, "el bot dejó de reproducir").
            if vc.connected:
                vc.queue.put_at(0, pista)
        else:
            est.reintentos.pop(cancion, None)
            est.caidas = decision.caidas
            log.warning(
                "Sin audio para %r después de %s intentos: %s",
                titulo, gastados, payload.exception,
            )
            if decision.accion is Accion.PARAR:
                # Con YouTube sin entregar, seguir probando solo quema la cola. Se deja
                # la cuenta de wavelink en su tope para que no pase al siguiente;
                # `!play` la retoma (ver `_arrancar_si_hace_falta`).
                vc._error_count = TOPE_DE_WAVELINK
                log.warning("%s temas seguidos sin audio: paro la cola", decision.caidas)
                if canal:
                    await canal.send(
                        f"No pude sacar el audio de **{titulo}** ni de los "
                        f"{decision.caidas - 1} anteriores: paro la cola. `!play` para retomar."
                    )
                return
            if canal:
                await canal.send(f"No pude sacar el audio de **{titulo}**, la salteo.")
            await self.locutor.decir(vc, self.salud.rendido(titulo))

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        vc: wavelink.Player = payload.player
        if not vc or not vc.guild:
            return

        est = self.estacion(vc.guild.id)
        est.cancelar_idle()
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

        # La URL del tema que viene se adelanta con lo que suena ya en marcha, para que el cambio no espere
        # a yt-dlp. Con una canción, unos `PRECARGAR_ANTES` s antes de que acabe. El corte del DJ entra a la
        # cola y se interpone: cuando arranca, `proximo` ya es la que viene tras él, y el rato de la voz es
        # justo el que hace falta para resolverla, así que ahí va enseguida. `precargar` deduplica.
        est.cancelar_precarga()
        if es_clip:
            self._precargar_siguiente(vc)
            await self._poner_letra(vc, sonando, es_clip=True)
            return
        est.precarga = asyncio.create_task(self._precargar_al_final(vc, est, sonando))

        # Que arranque una canción es la única prueba de que esto funciona. El corte
        # del DJ no cuenta: si contara, taparía para siempre que no suena música.
        self.salud.sono()

        self._programar_saltos(vc, sonando)

        # La letra se publica debajo del panel nuevo: el del tema anterior sale antes, para que no se quede arriba.
        await est.letra.parar()
        await self._post_panel(vc.guild, vc)
        await self._poner_letra(vc, sonando, es_clip=False)

        # El corte del próximo cambio se prepara ahora, con la canción sonando:
        # sintetizar la voz toma su tiempo y así llega listo.
        if self.dj.available():
            siguiente = proximo(vc)
            if siguiente is not None and not getattr(siguiente, "is_dj_clip", False):
                self._segundo_plano(self.locutor.encolar(vc, siguiente, self._session))

    # --- letra en vivo -------------------------------------------------------
    def _buscar_letra(self, pista: wavelink.Playable):
        """Lo que la letra en vivo llama para traer la de `pista`, por la caché que comparte con `/api/letra`."""
        titulo = resolver.titulo_de(pista)
        artista, cancion = lyrics.artist_and_song(titulo, getattr(pista, "author", "") or "")
        largo = int(getattr(pista, "length", 0) or 0)
        buscar = partial(lyrics.find_synced, self._session, titulo, artista, cancion, largo)
        return lambda: self.letras.obtener(resolver.clave_de_pista(pista), buscar)

    async def _poner_letra(self, vc: wavelink.Player, pista: wavelink.Playable, *, es_clip: bool) -> None:
        """Pone la letra en vivo al día con lo que suena: la del tema, o el aviso si habla el DJ. Apagada, no hace nada."""
        est = self.estacion(vc.guild.id)
        canal = getattr(vc, "text_channel", None)
        if not est.letra.activa or canal is None or pista is None:
            return
        if es_clip:
            await est.letra.dj(canal)
        else:
            est.letra.seguir(
                vc, pista, canal, titulo=resolver.titulo_de(pista), posicion=self.posicion_viva, obtener=self._buscar_letra(pista),
            )

    async def alternar_letra(self, guild: discord.Guild, modo: bool | None = None) -> str:
        """Enciende o apaga la letra en vivo (sin `modo`, alterna) y devuelve el aviso para quien lo pidió."""
        vc: wavelink.Player = guild.voice_client
        if not vc or not vc.current:
            return "No hay nada reproduciendo."
        est = self.estacion(guild.id)
        if not (modo if modo is not None else not est.letra.activa):
            await est.letra.apagar()
            return "🎤 Letra en vivo apagada."
        if getattr(vc, "text_channel", None) is None:
            return "No tengo un canal de texto donde publicar la letra."
        est.letra.activa = True
        await est.letra.parar()     # si ya estaba prendida, el mensaje vuelve a salir al final del chat
        await self._poner_letra(vc, vc.current, es_clip=self.locutor.es_clip(vc.current))
        return "🎤 Letra en vivo encendida: la voy editando en el chat línea a línea."

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

    @staticmethod
    def _cola_a_tocar(vc: wavelink.Player, incluir_autoplay: bool):
        """`queue` y, solo si se pide, `auto_queue`; None si no hay nada."""
        if vc.queue:
            return vc.queue
        return vc.auto_queue if incluir_autoplay and vc.auto_queue else None

    async def _arrancar_si_hace_falta(
        self, vc: wavelink.Player, incluir_autoplay: bool = False
    ) -> None:
        """Si no hay nada sonando, larga el primero de la cola.

        Va de a uno por servidor: dos `!play` seguidos (o uno que cae justo
        cuando el autoplay pasa de tema) veían los dos el player libre, sacaban
        cada uno un tema de la cola y el segundo pisaba al primero, que se
        perdía sin aviso. `incluir_autoplay` suma la `auto_queue`: solo lo usa el vigilante.
        """
        est = self.estacion(vc.guild.id)
        async with est.arranque:
            cola = self._cola_a_tocar(vc, incluir_autoplay)
            if self._ocupado(vc) or cola is None:
                return
            # Presentación del primer tema: aquí sí hay que esperar a que la voz
            # esté sintetizada, porque no hay nada sonando todavía.
            if self.dj.available():
                await self.locutor.encolar(vc, cola[0], self._session)
                # Pudo arrancar otro tema, y el corte queda al frente de `queue`.
                cola = self._cola_a_tocar(vc, incluir_autoplay)
                if self._ocupado(vc) or cola is None:
                    return
            # Lo pidió alguien: si la cola se había parado por fallos en cadena,
            # esto la retoma.
            vc._error_count = 0
            est.caidas = 0
            await vc.play(cola.get())

    # --- vigilante de la cola ------------------------------------------------
    def _players_del_bot(self) -> list[wavelink.Player]:
        # Con `bot=None` (los tests) no hay players.
        voces = getattr(self.bot, "voice_clients", ())
        return [vc for vc in voces if isinstance(vc, wavelink.Player)]

    async def _vigilar(self) -> None:
        """Revisa los servidores cada `VIGILAR_CADA`, mientras el cog esté cargado."""
        while True:
            await asyncio.sleep(VIGILAR_CADA)
            try:
                await self._vigilar_una_vez(self._players_del_bot())
            except Exception:  # noqa: BLE001 - un tropiezo no puede matar al vigilante
                log.exception("Falló una revisión del vigilante: sigo con la próxima")

    def _atascado(self, vc: wavelink.Player) -> bool:
        """Si el player está parado teniendo qué tocar y nadie lo detuvo a propósito."""
        if not vc.connected or vc.paused or self._ocupado(vc):
            return False
        if not (vc.queue or vc.auto_queue):
            return False
        # Lock tomado: hay un arranque en marcha (la voz del DJ sintetizándose).
        return not self.estacion(vc.guild.id).arranque.locked()

    async def _vigilar_una_vez(self, players) -> None:
        """Destraba los players atascados en dos revisiones seguidas (una sola puede ser un cambio de tema)."""
        anteriores = {g for g, est in self._estaciones.items() if est.atascado}
        ahora: set[int] = set()
        for vc in players:
            try:
                if getattr(vc, "guild", None) is None or not self._atascado(vc):
                    continue
                if vc.guild.id not in anteriores:
                    ahora.add(vc.guild.id)
                    continue
                # Sin volver a anotarlo: el próximo intento pide otras dos revisiones.
                await self._destrabar(vc)
            except Exception:  # noqa: BLE001 - un servidor roto no frena a los demás
                log.exception("No pude revisar el player de %s", getattr(vc, "guild", "?"))
        # Los que esta pasada no vio atascados (o no vio) pierden la marca.
        for g, est in self._estaciones.items():
            est.atascado = g in ahora

    async def _destrabar(self, vc: wavelink.Player) -> None:
        log.warning(
            "Nada suena en %s con temas esperando (%s en cola, %s del autoplay, "
            "sigue %r): lo destrabo",
            vc.guild, len(vc.queue), len(vc.auto_queue),
            resolver.titulo_de(proximo(vc)),
        )
        await self._arrancar_si_hace_falta(vc, incluir_autoplay=True)

    # --- colas en disco ------------------------------------------------------
    def _fotos(self) -> dict[int, dict]:
        """La foto de cada player conectado que tenga algo que retomar."""
        fotos = {}
        for vc in self._players_del_bot():
            try:
                if getattr(vc, "guild", None) is None or not vc.connected:
                    continue
                foto = persistencia.tomar(
                    vc, posicion=self._posicion(vc), es_clip=self.locutor.es_clip,
                    autoplay=self.autoplay_activo(vc.guild.id),
                )
            except Exception:  # noqa: BLE001 - un servidor roto no frena a los demás
                log.exception("No pude fotografiar la cola de %s", getattr(vc, "guild", "?"))
                continue
            if foto:
                fotos[vc.guild.id] = foto
        return fotos

    def _guardar(self, *, siempre: bool) -> None:
        """Escribe las colas de los players conectados; sin `siempre`, solo si algo cambió o pasó `REFRESCAR_CADA`."""
        fotos = self._fotos()
        huella = persistencia.huella(fotos)
        ahora = time.monotonic()
        if siempre or huella != self._huella or (fotos and ahora - self._escrita >= REFRESCAR_CADA):
            persistencia.guardar(fotos)
            self._huella, self._escrita = huella, ahora

    async def _guardar_en_bucle(self) -> None:
        """Fotografía las colas cada `GUARDAR_CADA`, mientras el cog esté cargado."""
        while True:
            await asyncio.sleep(GUARDAR_CADA)
            if self._persistencia != "activa":
                continue
            try:
                self._guardar(siempre=False)
            except Exception:  # noqa: BLE001 - un tropiezo no puede matar al guardador
                log.exception("No pude guardar las colas: sigo con la próxima")

    async def guardar_y_cerrar(self) -> None:
        """Escribe la foto final, deja de escribir y borra el panel y la letra en vivo de cada servidor.

        Hay que llamarlo antes de desconectar los players: sin ellos, la tarea
        periódica guardaría una foto vacía y se perdería la cola.
        """
        if self._persistencia == "activa":
            try:
                self._guardar(siempre=True)
            except Exception:  # noqa: BLE001 - cerrar tiene que seguir pase lo que pase
                log.exception("No pude guardar la foto final de las colas")
        self._persistencia = "cerrada"
        tarea, self._guardador = self._guardador, None
        if tarea:
            tarea.cancel()
            await asyncio.gather(tarea, return_exceptions=True)
        await self._borrar_mensajes()

    async def retomar_guardadas(self) -> None:
        """Retoma las colas que dejó guardadas el bot anterior; una sola vez por arranque."""
        if self._persistencia != "esperando":
            return
        self._persistencia = "retomando"
        try:
            # Sin la caché de servidores y canales no hay a dónde entrar.
            await self.bot.wait_until_ready()
            for guild_id, foto in persistencia.cargar().items():
                try:
                    await self._retomar_guardada(guild_id, foto)
                except Exception:  # noqa: BLE001 - un servidor roto no frena a los demás
                    log.exception("No pude retomar la cola de %s", guild_id)
        finally:
            if self._persistencia == "retomando":
                self._persistencia = "activa"

    async def _retomar_guardada(self, guild_id: int, foto: dict) -> None:
        """Entra al canal de voz de la foto y sigue por donde iba, si hay a quién tocarle."""
        guild = self.bot.get_guild(guild_id)
        canal = guild.get_channel(foto["voz"]) if guild else None
        if config.SERVIDORES_PERMITIDOS and guild_id not in config.SERVIDORES_PERMITIDOS:
            motivo = "no está en SERVIDORES_PERMITIDOS"
        elif guild is None:
            motivo = "el bot ya no está ahí"
        elif guild.voice_client:
            motivo = "ya hay una transmisión"
        elif canal is None:
            motivo = "el canal de voz ya no existe"
        elif all(m.bot for m in canal.members):
            motivo = "no hay nadie en el canal"
        else:
            motivo = None
        if motivo:
            log.info("No retomo la cola de %s: %s", guild_id, motivo)
            return
        vc = await canal.connect(cls=resolver.ResolvingPlayer)
        try:
            vc.resolver = self.resolver
            vc.text_channel = guild.get_channel_or_thread(foto["texto"]) if foto["texto"] else None
            self._autoplay[guild_id] = foto["autoplay"]
            self._aplicar_autoplay(vc)
            vc.queue.mode = wavelink.QueueMode.__members__.get(foto["modo"], wavelink.QueueMode.normal)
            await vc.set_volume(foto["volumen"])
            for datos in foto["cola"]:
                if pista := self._pista(datos):
                    await vc.queue.put_wait(pista)
            actual = self._pista(foto["actual"]) if foto["actual"] else None
            if actual:
                # Igual que `_retomar`: pasa por `ResolvingPlayer.play`, que pide una URL fresca.
                await vc.play(actual, start=foto["posicion"], paused=foto["pausado"], add_history=False)
            else:
                await self._arrancar_si_hace_falta(vc)
            if not self._ocupado(vc):
                raise RuntimeError("no quedó nada que tocar")
        except Exception:
            await self.teardown(guild)
            raise
        log.info("Retomé la cola de %s (%s temas esperando)", guild, len(vc.queue))
        if vc.text_channel:
            try:
                await vc.text_channel.send(AVISO_RETOMADA)
            except discord.HTTPException:
                log.debug("no pude avisar que retomé la cola", exc_info=True)

    @staticmethod
    def _pista(datos: dict) -> wavelink.Playable | None:
        """Rearma una pista guardada; None si ya no se puede."""
        try:
            pista = wavelink.Playable({k: v for k, v in datos.items() if k != persistencia.PEDIDO_POR})
        except Exception as error:  # noqa: BLE001 - viene de un archivo, de cualquier versión
            log.warning("Descarto una pista guardada que no se puede rearmar: %r", error)
            return None
        if persistencia.PEDIDO_POR in datos:
            pista.requester = datos[persistencia.PEDIDO_POR]
        return pista

    @commands.hybrid_command(name='play', aliases=['p'], description="Reproduce o encola un tema, una URL, una playlist o un link de Spotify")
    @app_commands.describe(query="Título, artista, URL de YouTube o link de Spotify")
    async def play(self, ctx: commands.Context, *, query: str):
        """Reproduce o encola una canción, una URL, una playlist o un link de Spotify."""
        await ctx.defer()
        vc = await self._connect_voice(ctx)
        if not vc:
            return

        self.estacion(ctx.guild.id).cancelar_idle()

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
                elif ctx.interaction:   # con `/` la respuesta diferida tiene que cerrarse con un mensaje
                    await ctx.send(f"▶️ **{track.title}**")

            await self._arrancar_si_hace_falta(vc)

    @commands.hybrid_command(name='playnext', aliases=['pn', 'next'], description="Mete un tema al principio de la cola: es el próximo en sonar")
    @app_commands.describe(query="Título, artista, URL de YouTube o link de Spotify (de una lista se toma el primero)")
    async def playnext(self, ctx: commands.Context, *, query: str):
        """Mete la canción al principio de la cola: es la próxima en sonar."""
        await ctx.defer()
        vc = await self._connect_voice(ctx)
        if not vc:
            return

        self.estacion(ctx.guild.id).cancelar_idle()

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
            if ctx.interaction:
                await ctx.send(f"▶️ **{track.title}**")
            return await self._arrancar_si_hace_falta(vc)

        # Al frente de todo, incluso de un corte del DJ que ya esté esperando:
        # el clip sigue pegado a la canción que presenta, que pasa a ser la
        # segunda.
        vc.queue.put_at(0, track)
        await ctx.send(f"🔝 La próxima: **{track.title}**")

    # --- autocompletado de /play y /playnext -----------------------------------------
    async def buscar_sugerencias(self, texto: str) -> list[app_commands.Choice[str]]:
        """Temas de YouTube Music para el autocompletado. No lanza: si Lavalink tarda o falla, no hay opciones."""
        texto = texto.strip()
        if len(texto) < SUGERENCIAS_MIN:
            return []
        if es_url(texto):   # Discord deja mandar lo escrito aunque no sea una opción; esta solo evita el "sin resultados"
            return [app_commands.Choice(name=texto, value=texto)] if len(texto) <= LARGO_DISCORD else []
        clave, ahora = texto.casefold(), time.monotonic()
        guardada = self._sugerencias.get(clave)
        if guardada and guardada[0] > ahora:
            return guardada[1]
        try:
            tracks = await asyncio.wait_for(wavelink.Playable.search(texto), SUGERENCIAS_ESPERA)
        except Exception:  # noqa: BLE001 - sin nodo, Lavalink caído o plazo vencido: sin opciones y ya
            log.debug("Sin sugerencias para %r", texto, exc_info=True)
            return []
        if isinstance(tracks, wavelink.Playlist):
            return []
        opciones = [o for o in map(opcion_de, tracks) if o][:SUGERENCIAS_MAX]
        if opciones:
            self._sugerencias = {k: v for k, v in self._sugerencias.items() if v[0] > ahora}
            while len(self._sugerencias) >= SUGERENCIAS_EN_CACHE:
                del self._sugerencias[next(iter(self._sugerencias))]
            self._sugerencias[clave] = (ahora + SUGERENCIAS_VIGENCIA, opciones)
        return opciones

    @play.autocomplete("query")
    @playnext.autocomplete("query")
    async def autocompletar_busqueda(self, interaction: discord.Interaction, current: str):
        """Sugerencias mientras se escribe. Discord no pasa el autocompletado por los checks, así que repite el de servidores."""
        permitidos = config.SERVIDORES_PERMITIDOS
        if interaction.guild_id is None or (permitidos and interaction.guild_id not in permitidos):
            return []
        return await self.buscar_sugerencias(current)

    # --- Spotify --------------------------------------------------------------
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
        primero = await cola_spotify.buscar_uno(lista.queries[0], pedido_por)
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
            est = self.estacion(ctx.guild.id)
            est.cancelar_spotify()
            est.spotify = asyncio.create_task(
                self._encolar_spotify(ctx, vc, restantes, pedido_por)
            )

    async def _encolar_spotify(
        self,
        ctx: commands.Context,
        vc: wavelink.Player,
        consultas: list[str],
        pedido_por: str,
    ) -> None:
        est = self.estacion(ctx.guild.id)
        try:
            carga = await cola_spotify.cargar(
                vc, consultas, pedido_por, self._espacio_en_cola, self._arrancar_si_hace_falta
            )
        finally:
            # Si otra lista reemplazó a esta, la referencia ya es de la nueva.
            if est.spotify is asyncio.current_task():
                est.spotify = None
        if carga is None:
            return

        resumen = f"🎧 Listo: {carga.encolados} temas más en la cola."
        if carga.perdidos:
            resumen += f" No encontré {carga.perdidos} en YouTube."
        try:
            await ctx.send(resumen)
        except discord.HTTPException:
            log.debug("no pude avisar el fin de la carga de Spotify", exc_info=True)

    @commands.hybrid_command(name='autoplay', aliases=['ap'], description="Sigue solo con temas parecidos cuando se acaba la cola")
    @app_commands.describe(modo="on o off; vacío muestra cómo está")
    @app_commands.choices(modo=[app_commands.Choice(name="Encender", value="on"), app_commands.Choice(name="Apagar", value="off")])
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

        await ctx.send(self.poner_autoplay(ctx.guild.id, ctx.voice_client, modo == "on"))

    # Solo con prefijo: elige con reacciones sobre un mensaje, que no encaja con una respuesta de `/`; `/play` ya autocompleta.
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
    @commands.hybrid_command(name='pause', description="Pausa lo que está sonando")
    async def pause(self, ctx: commands.Context):
        """Pausa lo que esté sonando."""
        msg = await controller.pause(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.hybrid_command(name='resume', aliases=['r'], description="Reanuda lo que estaba pausado")
    async def resume(self, ctx: commands.Context):
        """Reanuda lo que estaba pausado."""
        msg = await controller.resume(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.hybrid_command(name='skip', aliases=['s'], description="Salta a la canción siguiente")
    async def skip(self, ctx: commands.Context):
        """Salta a la canción siguiente."""
        msg = await controller.skip(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.hybrid_command(name='stop', description="Para todo, vacía la cola y sale del canal de voz")
    async def stop(self, ctx: commands.Context):
        """Para todo, vacía la cola y me salgo del canal."""
        await ctx.defer()
        if not ctx.voice_client:
            return await ctx.send("No estoy en ningún canal.")
        await self.teardown(ctx.guild)
        await ctx.send("Parado 🛑")

    @commands.hybrid_command(name='queue', aliases=['q'], description="Muestra la cola de reproducción")
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

    @commands.hybrid_command(name='nowplaying', aliases=['np'], description="Dice qué canción está sonando")
    async def nowplaying(self, ctx: commands.Context):
        """Dice qué canción está sonando."""
        vc: wavelink.Player = ctx.voice_client
        if not vc or not vc.current:
            return await ctx.send("No hay nada reproduciendo.")
        t = vc.current
        tag = " 🔁" if vc.queue.mode == wavelink.QueueMode.loop else ""
        req = getattr(t, "requester", None) or "autoplay"
        await ctx.send(
            f"🎵 **{t.title}**{tag}\n"
            f"Duración: `{format_duration(t.length)}` — Pedido por: {req}"
        )

    @commands.hybrid_command(name='panel', description="Vuelve a publicar el panel de botones")
    async def panel_cmd(self, ctx: commands.Context):
        """Vuelve a postear el panel de botones."""
        await ctx.defer(ephemeral=True)
        vc: wavelink.Player = ctx.voice_client
        if not vc or not vc.current:
            return await ctx.send("No hay nada reproduciendo.")
        await self._post_panel(ctx.guild, vc)
        if ctx.interaction:   # el panel va al canal de texto; aquí solo se cierra la respuesta
            await ctx.send("Panel publicado.")

    @en_el_mismo_canal()
    @commands.hybrid_command(name='loop', aliases=['l'], description="Activa o desactiva la repetición de la canción actual")
    async def loop_cmd(self, ctx: commands.Context):
        """Enciende o apaga la repetición de la canción actual."""
        msg = await controller.toggle_loop(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.hybrid_command(name='shuffle', description="Mezcla el orden de la cola")
    async def shuffle(self, ctx: commands.Context):
        """Mezcla el orden de la cola."""
        msg = controller.shuffle(ctx.voice_client)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.hybrid_command(name='volume', aliases=['vol'], description="Ajusta el volumen del bot")
    @app_commands.describe(vol="Volumen de 0 a 100")
    async def volume(self, ctx: commands.Context, vol: int):
        """Ajusta el volumen, de 0 a 100."""
        msg = await controller.set_volume(ctx.voice_client, vol)
        await ctx.send(msg)

    @en_el_mismo_canal()
    @commands.hybrid_command(name='remove', aliases=['rm'], description="Saca de la cola la canción en esa posición")
    @app_commands.describe(pos="Posición en la cola, empezando por 1")
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

    @commands.hybrid_command(name='dj', description="El DJ de radio: habla en el próximo corte, se enciende o apaga, o ajusta su voz")
    @app_commands.describe(opcion="on, off o vol; vacío hace que hable en el próximo corte", valor="Con vol: volumen de la voz del DJ, de 0 a 200")
    @app_commands.choices(opcion=[app_commands.Choice(name="Encender", value="on"), app_commands.Choice(name="Apagar", value="off"), app_commands.Choice(name="Volumen de su voz", value="vol")])
    async def dj_cmd(self, ctx: commands.Context, opcion: str = "", valor: str = ""):
        """El DJ de radio: `!dj` habla en el próximo corte, `!dj on|off` lo enciende o apaga, `!dj vol 0-200` ajusta su voz."""
        if not opcion:
            if not self.dj.available():
                return await ctx.send("El DJ está apagado o falta espeak-ng.")
            self.dj.force(ctx.guild.id)
            return await ctx.send("🎙️ El DJ habla en el próximo corte.")

        opcion = opcion.lower()
        if opcion in ("on", "off"):
            if opcion == "on" and not tts.available():
                return await ctx.send("No puedo: falta `espeak-ng` en el sistema.")
            self.dj.enabled = opcion == "on"
            return await ctx.send(f"DJ {'activado 🎙️' if self.dj.enabled else 'apagado'}")

        if opcion in ("vol", "volumen"):
            if not valor:
                return await ctx.send(f"La voz del DJ está al **{self.dj.volume}%**. Usa `!dj vol 0-200`.")
            try:
                pedido = int(valor)
            except ValueError:
                return await ctx.send("El volumen del DJ va entre 0 y 200.")
            aplicado = self.dj.set_volume(pedido)
            if aplicado == 0:
                return await ctx.send("DJ muteado 🔇 (`!dj vol 100` lo devuelve).")
            # El clip se sintetiza mientras suena el tema anterior, así que el
            # que ya está listo sale con el volumen viejo.
            return await ctx.send(f"Voz del DJ al **{aplicado}%** 🎚️ (se escucha desde el próximo corte).")

        await ctx.send("Uso: `!dj`, `!dj on|off`, `!dj vol 0-200`")

    @en_el_mismo_canal()
    @commands.hybrid_command(name='letra', description="Muestra la letra en vivo en el chat, línea a línea, mientras suena el tema")
    @app_commands.describe(modo="on u off; vacío alterna")
    @app_commands.choices(modo=[app_commands.Choice(name="Encender", value="on"), app_commands.Choice(name="Apagar", value="off")])
    async def letra_cmd(self, ctx: commands.Context, modo: str = ""):
        """La letra en vivo: `!letra` alterna, `!letra on|off` la enciende o la apaga. `!lyrics` manda la letra entera."""
        modo = modo.lower()
        if modo not in ("", "on", "off"):
            return await ctx.send("Uso: `!letra`, `!letra on` o `!letra off`.", ephemeral=True)
        await ctx.send(await self.alternar_letra(ctx.guild, {"on": True, "off": False}.get(modo)), ephemeral=True)

    @commands.hybrid_command(name='lyrics', aliases=['ly'], description="Busca la letra de la canción actual o de la que indiques")
    @app_commands.describe(query="Artista - canción; vacío busca la que está sonando")
    async def lyrics_cmd(self, ctx: commands.Context, *, query: str = ""):
        """Busca la letra de la canción actual (o la que le pidas)."""
        await ctx.defer()
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
