"""El audio de YouTube lo resuelve yt-dlp; Lavalink solo lo reproduce.

**Por qué existe este módulo.** El plugin `youtube-source` de Lavalink dejó de
poder sacar el audio: sus clientes o piden login, o reciben un `400`, o les
llega SABR —el formato nuevo de YouTube— que lavaplayer no entiende. No hay
versión del plugin que lo arregle: 1.18.2 es la última publicada. yt-dlp, en
cambio, sigue resolviendo sin credenciales; se comprobó pidiendo el mismo video
con los dos, uno al lado del otro.

**Cómo funciona.** Antes de reproducir, se le pide a yt-dlp la URL directa del
audio y se le pasa esa URL a Lavalink por su fuente `http`. Lavalink queda
haciendo lo que sí hace bien —transcodificar y mandar el audio a Discord— y la
parte que YouTube rompe cada dos por tres queda del lado de yt-dlp, que saca
versión nueva apenas YouTube cambia algo.

La URL se pide **en el momento de reproducir**, nunca al encolar: las de
googlevideo caducan en unas horas y una cola larga las mataría antes de que les
llegue el turno.

**La única excepción es el tema que viene.** Pedir la URL y cargarla en Lavalink
tarda segundos, y son el silencio que se oye entre canciones. Por eso, mientras
suena un tema, el cog pide `precargar()` del siguiente: la URL se adelanta unos
minutos, no horas, y solo de esa pista. Cada precarga se usa una vez y solo si le
queda vida de sobra para toda la canción; si no, se resuelve como siempre.

El título, el autor y la carátula que muestra el panel salen de la pista
original de YouTube: la fuente `http` no trae metadatos y sin esto todo se
vería como "Unknown title".

**Por qué el estado vive en una instancia.** Lo que cambia con el tiempo —los
videos que no van a sonar, la versión de yt-dlp y el cliente de YouTube que se
le pide— está en `Resolver` y no en variables del módulo. Así cada test arma el
suyo sin limpiar el de otro, y el cliente se puede cambiar en caliente cuando el
actual deja de funcionar, sin reiniciar el bot. Las funciones que solo leen sus
argumentos se quedan sueltas en el módulo.

**Lo que se arregla solo.** Si `direct_url` falla varias veces seguidas por motivos
que no son del video, se sondean en segundo plano los demás clientes y se pasa al
primero que entrega audio; si ninguno sirve, o yt-dlp está viejo, se actualiza. El
cliente cambiado así dura hasta reiniciar: para fijarlo va en `YTDLP_PLAYER_CLIENT`.
"""

import asyncio
import copy
import datetime
import logging
import os
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

import wavelink

from core import config

log = logging.getLogger("resolver")

# El cliente `android` entrega el itag 18 (mp4 con audio). Se pide primero el
# mejor audio suelto por si algún día vuelve a haber formatos separados.
FORMATO = "bestaudio/18/best"

# Campos que se le devuelven a la pista http para que se vea como la de YouTube.
# `identifier` va incluido a propósito: es la clave con la que el cog cuenta los
# reintentos y con la que el DJ reconoce de qué canción está hablando.
METADATOS = ("title", "author", "identifier", "uri", "artworkUrl", "length", "isrc")

# Lo mismo, pero guardado en `userData`, que es lo ÚNICO que sobrevive el viaje
# a Lavalink y vuelta: `Player.play()` manda el `encoded` y el `userData`, nada
# más. Todo lo que pisamos en `info` vive solo en este proceso, así que cuando la
# pista vuelve en un evento de excepción llega con la cara de la fuente http
# ("Unknown title", uri de googlevideo). Sin esta copia no habría manera de saber
# de qué video de YouTube salía, y el reintento seguiría pidiendo la URL muerta.
CLAVE_ORIGEN = "yt_origen"
ORIGEN = ("uri", "identifier", "title", "author", "artworkUrl", "length", "isrc")

# Lavalink a veces no logra abrir una URL de googlevideo que acaba de resolverse
# (`Read timed out`, `302`, `EOFException`) y a la vuelta siguiente la abre sin
# problema. Se insiste con la misma URL antes de caer al plugin, que siempre
# falla pidiendo login y solo gasta el turno de reintento.
INTENTOS_DE_CARGA = 3
PAUSA_ENTRE_CARGAS = 1.0

# Fallos de yt-dlp que no mejoran reintentando. El orden importa: un "removed by
# the uploader" también dice "Video unavailable", y lo que se quiere decir es lo
# más específico. El texto de la derecha lo termina diciendo el DJ en voz, así
# que va corto y en castellano, no como código de error.
MOTIVOS_DEFINITIVOS = (
    ("blocked due to the claimed content", "está bloqueada"),
    ("blocked it in your country", "está bloqueada en el país"),
    ("removed by the uploader", "la borraron"),
    ("has been removed", "la borraron"),
    ("private video", "es privada"),
    ("available to this channel's members", "es solo para miembros"),
    ("video unavailable", "no está disponible"),
)

# Cuántos videos sin caso recuerda cada `Resolver`. Acotada: es una ayuda para no
# quemar reintentos, no un registro histórico.
RECUERDO_MAXIMO = 100

# Cuántas URLs adelantadas guarda cada `Resolver`. El cog pide una por cada tema
# que arranca (la que viene) y otra cuando arranca el corte del DJ (la que viene
# después de él), así que casi nunca hay más de dos vivas; el resto del tope es
# holgura por si alguien reordena la cola. Al pasarse se tira la más vieja: es la
# que más probablemente quedó sin uso porque la sacaron de la cola.
PRECARGAS_MAXIMAS = 4

# Si la URL no trae `expire` no se sabe cuándo caduca. Se supone poco (lo normal
# en googlevideo son horas): equivocarse hacia lo corto solo cuesta volver a
# pedirla, y hacia lo largo corta la canción a la mitad.
VIDA_SIN_EXPIRE = 30 * 60

# Cuánto le tiene que sobrar a una precarga, además de lo que dura la canción,
# para usarla. No basta con que siga viva al arrancar: Lavalink puede volver a
# abrir la URL a mitad de tema (un `seek`, un corte de red, un `Read timed out`
# que se reintenta) y si ya venció, ese segundo intento falla con el tema sonando.
MARGEN_DE_VENCIMIENTO = 10 * 60

# yt-dlp es el punto único de falla de toda la música y nadie lo mira. Su
# versión es una fecha, así que la antigüedad se calcula sin pedirle nada a la
# red: si quedó viejo, se ve en el panel.
DIAS_PARA_QUE_ESTE_VIEJO = 30

# El video con el que se prueba un cliente: uno estable, para que si falla sea culpa del cliente.
VIDEO_DE_PRUEBA = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Uno o dos fallos son normales (un 400 suelto); tres seguidos ya parecen un cliente cerrado.
FALLOS_PARA_SONDEAR = 3

# Un sondeo cuesta varias llamadas a yt-dlp: si YouTube cae para todos, no se repite cada canción.
SONDEO_CADA = 10 * 60

TIMEOUT_DE_ACTUALIZACION = 180

# Haya salido bien o no: si yt-dlp no se deja actualizar, no se reintenta en bucle.
ACTUALIZAR_CADA = 6 * 3600

# El bot puede pasar semanas arriba y el aviso de "viejo" tiene que llegar a verse.
REVISAR_CADA = 24 * 3600


def _clasificar(error: str) -> str | None:
    """El motivo en castellano si el fallo es definitivo, o None si es pasajero."""
    plano = error.lower()
    for fragmento, motivo in MOTIVOS_DEFINITIVOS:
        if fragmento in plano:
            return motivo
    return None


def _resumen_url(url: str) -> str:
    """Host, itag y caducidad de una URL de googlevideo, sin la firma."""
    consulta = parse_qs(urlparse(url).query)
    caduca = consulta.get("expire", ["?"])[0]
    return f"host={urlparse(url).hostname} itag={consulta.get('itag', ['?'])[0]} expire={caduca}"


def _vencimiento(url: str) -> float | None:
    """El momento en que caduca la URL (epoch en segundos), o None si no lo dice.

    googlevideo lo pone en el parámetro `expire`, que es una hora epoch entera.
    """
    try:
        return float(int(parse_qs(urlparse(url).query)["expire"][0]))
    except (KeyError, IndexError, ValueError):
        return None


def _duracion_en_s(pista: wavelink.Playable) -> float:
    """Lo que dura la canción, en segundos; 0 si no se sabe.

    `length` viene en milisegundos y puede faltar o ser 0.
    """
    try:
        return max(0.0, float(getattr(pista, "length", 0) or 0)) / 1000
    except (TypeError, ValueError):
        return 0.0


def _origen(pista: wavelink.Playable) -> dict:
    """De qué video de YouTube salió esta pista. Vacío si no se sabe."""
    try:
        extras = dict(getattr(pista, "extras", None) or {})
    except (TypeError, ValueError):
        return {}
    guardado = extras.get(CLAVE_ORIGEN)
    return guardado if isinstance(guardado, dict) else {}


def id_de_cancion(pista: wavelink.Playable) -> str:
    """Con qué identidad contar los reintentos de esta pista.

    No sirve el `identifier` propio: cada resolución trae otra URL de
    googlevideo y con ella otro identifier, así que el contador se reiniciaría
    en cada vuelta y la canción no se saltearía nunca. El id de YouTube, en
    cambio, es el mismo en todas las vueltas.
    """
    return _origen(pista).get("identifier") or getattr(pista, "identifier", "")


def _pagina_de(pista: wavelink.Playable) -> str:
    """La página de YouTube de la que sale esta pista; vacío si no es de YouTube.

    Una pista de YouTube se resuelve por su uri; una ya resuelta (el reintento)
    llega con la uri de googlevideo, así que la buena sale del origen guardado.
    """
    if getattr(pista, "source", None) == "youtube":
        return pista.uri or ""
    return _origen(pista).get("uri") or ""


def _hoy() -> datetime.date:
    """Aparte para que los tests puedan pararse en un día fijo."""
    return datetime.date.today()


def antiguedad_de(version: str, hoy: datetime.date | None = None) -> int | None:
    """Días desde que salió esa versión, o None si no se entiende el número.

    Las versiones de yt-dlp son fechas (`2026.07.04`), a veces con cola de
    desarrollo pegada atrás.
    """
    partes = (version or "").split(".")[:3]
    if len(partes) < 3:
        return None
    try:
        salida = datetime.date(int(partes[0]), int(partes[1]), int(partes[2][:2]))
    except ValueError:
        return None
    return ((hoy or _hoy()) - salida).days


def titulo_de(pista: wavelink.Playable) -> str:
    """Cómo se llama esta canción para quien la escucha.

    La pista que vuelve de Lavalink en un evento se llama "Unknown title": la
    fuente `http` no trae metadatos y los que pisamos no viajan. El nombre bueno
    está en el origen guardado.
    """
    return _origen(pista).get("title") or getattr(pista, "title", "") or "la canción"


def _con_la_cara_del_original(
    pista_http: wavelink.Playable, original: wavelink.Playable
) -> wavelink.Playable:
    """La pista que suena (http) con los datos que se ven (los de YouTube)."""
    datos = copy.deepcopy(pista_http.raw_data)
    # De dónde salen los datos que se ven: del origen guardado si esta pista ya
    # pasó por aquí (o sea, es un reintento), y de la pista de YouTube si es el
    # primer play. En el reintento NO se le puede copiar la cara a partir de sí
    # misma: la que vuelve de Lavalink ya viene con "Unknown title".
    cara = _origen(original) or original.raw_data.get("info", {})
    for campo in METADATOS:
        if campo in cara:
            datos["info"][campo] = cara[campo]
    # `sourceName` dice "youtube" aunque el audio salga por http. No es cosmético:
    # el AutoPlay de wavelink arma sus recomendaciones con
    # `[t.identifier for t in seeds if t.source == "youtube"]`, así que con "http"
    # se queda sin semillas, sin query y sin próxima canción.
    #
    # Re-resolverla es justo lo que se busca en el reintento, y sale bien porque
    # la uri que la acompaña es la de YouTube. A Lavalink esto no le llega: solo le viaja el
    # `encoded`, que sigue siendo el del stream http.
    datos["info"]["sourceName"] = "youtube"
    # El origen se vuelve a escribir para que aguante todas las vueltas que dé.
    datos.setdefault("userData", {})[CLAVE_ORIGEN] = {
        campo: cara[campo] for campo in ORIGEN if campo in cara
    }
    return wavelink.Playable(datos)


class _Intento(NamedTuple):
    """Cómo le fue a una llamada a yt-dlp que pedía la URL de un video."""

    url: str | None
    # De quién es la falta si no hubo URL: "cliente" (error sin motivo claro o salida sin URL),
    # "video" (motivo definitivo) o "entorno" (no se pudo lanzar o se pasó de tiempo: red o binario).
    culpa: str | None = None
    motivo: str | None = None


class Resolver:
    """Le pide a yt-dlp el audio de YouTube y lleva la cuenta de cómo le va.

    Hay una sola instancia por bot (`bot.resolver`), compartida por el cog y por
    los players de cada servidor. Lo que se recuerda aquí —qué videos no van a
    sonar, qué versión de yt-dlp hay, qué cliente de YouTube se usa— tiene que ser
    visto por todos, y por eso no se crea una por player.

    Los parámetros en `None` toman el valor de `core.config` al construir, no al
    importar el módulo. `reloj` da la hora monotónica de los plazos (inyectable en tests).
    """

    def __init__(
        self,
        *,
        binario: str | None = None,
        cliente: str | None = None,
        timeout: int | None = None,
        clientes: tuple[str, ...] | None = None,
        autoactualizar: bool | None = None,
        reloj: Callable[[], float] | None = None,
    ):
        self.binario = config.YTDLP_BIN if binario is None else binario
        # Público y modificable: cuando YouTube cierra un cliente, quien lo note
        # puede pasar a otro y la siguiente llamada a yt-dlp ya lo usa.
        self.cliente = config.YTDLP_PLAYER_CLIENT if cliente is None else cliente
        # Con el que arrancó: los paneles lo comparan con `cliente` para avisar del cambio.
        self.cliente_configurado = self.cliente
        self.clientes = tuple(config.YTDLP_CLIENTES if clientes is None else clientes)
        self.autoactualizar = config.YTDLP_AUTOACTUALIZAR if autoactualizar is None else autoactualizar
        self.timeout = config.YTDLP_TIMEOUT if timeout is None else timeout
        self._reloj = reloj or time.monotonic
        # Los videos que ya sabemos que no van a funcionar, por uri de YouTube.
        self._sin_caso: "OrderedDict[str, str]" = OrderedDict()
        self._estado_ytdlp: dict = {"version": None, "dias": None, "viejo": False}
        # Las URLs adelantadas, por id de YouTube, de la más vieja a la más nueva.
        # Guardan la tarea y no su resultado porque `resolve()` tiene que poder
        # esperar a una que sigue en curso. También son la única referencia fuerte
        # a esas tareas: el loop solo guarda débiles y se las llevaría el GC.
        self._precargas: "OrderedDict[str, asyncio.Task]" = OrderedDict()
        # Fallos pasajeros de `direct_url` desde el último éxito.
        self._fallos_seguidos = 0
        # Referencia fuerte al sondeo en curso: el loop solo guarda débiles.
        self._sondeo: asyncio.Task | None = None
        # Hora de `_reloj` del último sondeo y del último intento de actualizar (None = ninguno).
        self._ultimo_sondeo: float | None = None
        self._ultima_actualizacion: float | None = None
        # Libre salvo durante la actualización. Evento y no lock: fuera de ella las resoluciones van en paralelo.
        self._ytdlp_libre = asyncio.Event()
        self._ytdlp_libre.set()
        # Llamadas a yt-dlp en curso: en Windows pip no puede reemplazar un .exe que sigue corriendo.
        self._llamadas = 0
        self._sin_llamadas = asyncio.Event()
        self._sin_llamadas.set()

    def _extractor_args(self, cliente: str) -> str:
        """El cliente de YouTube que se le pide a yt-dlp; quien llama lo lee de `self.cliente` en cada llamada.

        Sin esto yt-dlp usa sus clientes por defecto y se come el mismo "Sign in
        to confirm you're not a bot" que tumbó al plugin.
        """
        return f"youtube:player_client={cliente}"

    def olvidar_motivos(self) -> None:
        """Vacía la tabla de videos sin caso."""
        self._sin_caso.clear()

    def motivo_definitivo(self, pista: wavelink.Playable) -> str | None:
        """Por qué esta canción no va a sonar nunca, si ya lo sabemos.

        Se busca por la uri de YouTube: en el reintento la pista llega con la de
        googlevideo, así que primero se mira el origen guardado.
        """
        uri = _origen(pista).get("uri") or getattr(pista, "uri", "") or ""
        return self._sin_caso.get(uri)

    async def direct_url(self, webpage_url: str) -> str | None:
        """La URL directa del audio, o None si yt-dlp no pudo.

        Cuenta los fallos del cliente: un éxito la pone a cero y solo suman los pasajeros.
        """
        if not webpage_url:
            return None
        cliente = self.cliente
        intento = await self._pedir_url(webpage_url, cliente)
        if intento.url:
            self._fallos_seguidos = 0
            return intento.url
        if intento.culpa == "video":
            self._anotar_sin_caso(webpage_url, intento.motivo)
        # Si el cliente cambió mientras corría, el fallo es de uno que ya no se usa.
        elif intento.culpa == "cliente" and cliente == self.cliente:
            self._contar_fallo()
        return None

    def _anotar_sin_caso(self, webpage_url: str, motivo: str | None) -> None:
        self._sin_caso[webpage_url] = motivo
        self._sin_caso.move_to_end(webpage_url)
        while len(self._sin_caso) > RECUERDO_MAXIMO:
            self._sin_caso.popitem(last=False)

    async def _pedir_url(self, webpage_url: str, cliente: str) -> _Intento:
        """Una llamada a yt-dlp pidiendo la URL de audio con ese cliente; no toca contador ni tabla sin caso."""
        # Con una actualización en curso, yt-dlp puede estar a medio instalar.
        await self._ytdlp_libre.wait()
        self._llamadas += 1
        self._sin_llamadas.clear()
        try:
            return await self._llamar_ytdlp(webpage_url, cliente)
        finally:
            self._llamadas -= 1
            if not self._llamadas:
                self._sin_llamadas.set()

    async def _llamar_ytdlp(self, webpage_url: str, cliente: str) -> _Intento:
        # Nunca por shell: los títulos y las URLs vienen de YouTube y no son de fiar.
        try:
            proceso = await asyncio.create_subprocess_exec(
                self.binario,
                "--no-warnings",
                "--skip-download",
                "--no-playlist",
                "--extractor-args", self._extractor_args(cliente),
                "-f", FORMATO,
                "--get-url",
                webpage_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            # No existe (FileNotFoundError) o no se puede ejecutar (PermissionError).
            # Sin URL, y `resolve()` cae a la pista original, como promete.
            log.error("No pude lanzar yt-dlp (%s): %s", self.binario, error)
            return _Intento(None, "entorno")
        try:
            salida, error = await asyncio.wait_for(proceso.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proceso.kill()
            await proceso.wait()
            log.warning("yt-dlp se pasó de %ss con %s (cliente %s)", self.timeout, webpage_url, cliente)
            return _Intento(None, "entorno")
        except asyncio.CancelledError:
            # Una precarga descartada se cancela en plena resolución: sin esto el
            # yt-dlp seguiría corriendo huérfano hasta terminar solo.
            try:
                proceso.kill()
            except ProcessLookupError:
                pass
            raise
        if proceso.returncode != 0:
            detalle = error.decode(errors="replace").strip()[:200]
            log.warning("yt-dlp no pudo con %s (cliente %s): %s", webpage_url, cliente, detalle)
            motivo = _clasificar(detalle)
            return _Intento(None, "video", motivo) if motivo else _Intento(None, "cliente")
        # `--get-url` imprime una línea por formato; con un formato pedido es una.
        for linea in salida.decode(errors="replace").splitlines():
            linea = linea.strip()
            if linea.startswith("http"):
                return _Intento(linea)
        return _Intento(None, "cliente")

    def _contar_fallo(self) -> None:
        """Suma un fallo pasajero y, con `FALLOS_PARA_SONDEAR` seguidos, lanza el sondeo suelto.

        La resolución en curso no lo espera, no hay dos a la vez y entre uno y otro pasa `SONDEO_CADA`.
        """
        self._fallos_seguidos += 1
        if self._fallos_seguidos < FALLOS_PARA_SONDEAR:
            return
        if self._sondeo is not None and not self._sondeo.done():
            return
        ahora = self._reloj()
        if self._ultimo_sondeo is not None and ahora - self._ultimo_sondeo < SONDEO_CADA:
            return
        self._ultimo_sondeo = ahora
        log.warning(
            "yt-dlp falló %s veces seguidas con el cliente %r: pruebo los demás en segundo plano",
            self._fallos_seguidos, self.cliente,
        )
        self._sondeo = asyncio.create_task(self._sondear())

    async def _sondear(self) -> None:
        """Busca un cliente que dé audio; si ninguno, actualiza yt-dlp y prueba otra vez, empezando por el actual.

        Corre suelta: lo que pase se queda en el log, no se pierde como tarea sin recuperar.
        """
        try:
            if await self._rotar_cliente():
                return
            log.error(
                "Ningún cliente de YouTube entregó audio (el actual es %r y probé: %s)",
                self.cliente, ", ".join(c for c in self.clientes if c != self.cliente) or "ninguno",
            )
            if not self.autoactualizar:
                log.error("La actualización automática está apagada (YTDLP_AUTOACTUALIZAR): actualiza yt-dlp a mano.")
            elif await self.actualizar_ytdlp() and not await self._rotar_cliente(con_el_actual=True):
                log.error("Con yt-dlp actualizado sigue sin sonar ningún cliente: diagnostícalo a mano (ver AGENTS.md).")
        except Exception:  # noqa: BLE001
            log.exception("Falló el sondeo de clientes de YouTube")

    async def _rotar_cliente(self, *, con_el_actual: bool = False) -> bool:
        """Se pasa al primer cliente de `self.clientes` que entregue URL del video de prueba.

        Salta el actual, salvo con `con_el_actual`, que lo prueba primero. Las pruebas no cuentan
        como fallos ni éxitos de `direct_url`.
        """
        candidatos = [c for c in self.clientes if c != self.cliente]
        if con_el_actual:
            candidatos.insert(0, self.cliente)
        for candidato in candidatos:
            log.info("Probando el cliente de YouTube %r", candidato)
            if not (await self._pedir_url(VIDEO_DE_PRUEBA, candidato)).url:
                continue
            anterior, self.cliente = self.cliente, candidato
            # Al cliente nuevo no se le heredan los fallos del anterior.
            self._fallos_seguidos = 0
            if anterior == candidato:
                log.info("El cliente de YouTube %r vuelve a dar audio", candidato)
            else:
                log.warning(
                    "El cliente de YouTube %r dejó de dar audio: paso a %r. Dura hasta que "
                    "reinicies el bot; para que quede fijo pon YTDLP_PLAYER_CLIENT=%s en el .env.",
                    anterior, candidato, candidato,
                )
            return True
        return False

    def _comando_de_actualizacion(self) -> list[str]:
        """pip si el binario está junto al python del bot (el venv); si no, `-U` del propio binario.

        Las rutas se comparan sin `resolve()`: en Linux el python del venv es un symlink al del sistema.
        """
        def carpeta(ruta: str) -> str:
            return os.path.normcase(os.path.dirname(os.path.abspath(ruta)))
        if carpeta(self.binario) == carpeta(sys.executable):
            return [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
        return [self.binario, "-U"]

    async def actualizar_ytdlp(self) -> bool:
        """Actualiza yt-dlp y devuelve si salió bien.

        Entre intentos pasa al menos `ACTUALIZAR_CADA`, salga como salga. Mientras corre, `_pedir_url` espera.
        """
        ahora = self._reloj()
        if self._ultima_actualizacion is not None and ahora - self._ultima_actualizacion < ACTUALIZAR_CADA:
            log.info("No actualizo yt-dlp: el último intento fue hace %.0f min", (ahora - self._ultima_actualizacion) / 60)
            return False
        # Se anota antes de empezar: con dos llamadas a la vez, la segunda no lanza otro pip.
        self._ultima_actualizacion = ahora
        comando = self._comando_de_actualizacion()
        antes = self._estado_ytdlp["version"]
        log.info("Actualizando yt-dlp (era %s): %s", antes or "desconocido", " ".join(comando))
        self._ytdlp_libre.clear()
        try:
            try:
                await asyncio.wait_for(self._sin_llamadas.wait(), timeout=self.timeout + 5)
            except asyncio.TimeoutError:
                log.warning("Sigue corriendo un yt-dlp: actualizo igual")
            salio_bien = await self._correr_actualizacion(comando)
        finally:
            # Aun cancelada: dejarlo cerrado colgaría para siempre las resoluciones.
            self._ytdlp_libre.set()
        if not salio_bien:
            return False
        despues = (await self.revisar_ytdlp())["version"]
        if despues == antes:
            log.info("yt-dlp ya estaba en la última versión (%s)", despues)
        else:
            log.info("yt-dlp actualizado: %s -> %s", antes or "desconocido", despues)
        return True

    async def _correr_actualizacion(self, comando: list[str]) -> bool:
        """Corre el comando, sin shell y con tiempo límite."""
        try:
            proceso = await asyncio.create_subprocess_exec(
                *comando, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        except OSError as error:
            log.error("No pude lanzar la actualización de yt-dlp (%s): %s", comando[0], error)
            return False
        try:
            salida, _ = await asyncio.wait_for(proceso.communicate(), timeout=TIMEOUT_DE_ACTUALIZACION)
        except asyncio.TimeoutError:
            proceso.kill()
            await proceso.wait()
            log.error("La actualización de yt-dlp se pasó de %ss: la corté", TIMEOUT_DE_ACTUALIZACION)
            return False
        except asyncio.CancelledError:
            # El bot se cierra: no se deja pip corriendo huérfano.
            try:
                proceso.kill()
            except ProcessLookupError:
                pass
            raise
        if proceso.returncode != 0:
            # Lo que importa del error está al final.
            detalle = salida.decode(errors="replace").strip()[-300:]
            log.error("No pude actualizar yt-dlp (%s salió con %s): %s", comando[0], proceso.returncode, detalle)
            return False
        return True

    async def mantener_al_dia(self) -> None:
        """Revisa la versión de yt-dlp al arrancar y cada `REVISAR_CADA`, y la actualiza si está vieja.

        Con `autoactualizar` apagado solo avisa. Se cancela al cerrar el bot.
        """
        while True:
            try:
                await self.revisar_ytdlp()
                if self.autoactualizar and self._estado_ytdlp["viejo"]:
                    await self.actualizar_ytdlp()
            except Exception:  # noqa: BLE001
                # Si la tarea muriera, nadie volvería a mirar la versión.
                log.exception("Falló la revisión de yt-dlp")
            await asyncio.sleep(REVISAR_CADA)

    async def revisar_ytdlp(self) -> dict:
        """Le pregunta la versión a yt-dlp y la deja anotada para el panel."""
        await self._ytdlp_libre.wait()
        version = None
        proceso = None
        try:
            proceso = await asyncio.create_subprocess_exec(
                self.binario, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            salida, _ = await asyncio.wait_for(proceso.communicate(), timeout=10)
            if proceso.returncode == 0:
                version = salida.decode(errors="replace").strip() or None
        except (OSError, asyncio.TimeoutError):
            if proceso is not None and proceso.returncode is None:
                proceso.kill()
                await proceso.wait()
            log.warning("no pude preguntarle la versión a yt-dlp")

        dias = antiguedad_de(version or "")
        self._estado_ytdlp.update(
            version=version,
            dias=dias,
            viejo=dias is not None and dias > DIAS_PARA_QUE_ESTE_VIEJO,
        )
        if self._estado_ytdlp["viejo"]:
            log.warning("yt-dlp está en %s, hace %s días. Conviene actualizarlo.", version, dias)
        return dict(self._estado_ytdlp)

    def estado_ytdlp(self) -> dict:
        """Lo último que se supo de yt-dlp y del cliente (actual y configurado). Lo lee la API para el panel."""
        return {**self._estado_ytdlp, "cliente": self.cliente, "cliente_configurado": self.cliente_configurado}

    async def resolve(self, pista: wavelink.Playable) -> wavelink.Playable:
        """La pista lista para reproducir. Si algo falla, devuelve la original.

        Hay dos maneras de llegar aquí y las dos tienen que terminar con una URL
        fresca:

        - **La pista viene de YouTube**: es el primer play. Se resuelve por su uri y
          de ahí sale el origen que se guarda en `userData`.
        - **La pista ya está resuelta y vuelve a pasar**: es el reintento después de
          un fallo. Llega de Lavalink con la cara de la fuente http y la uri de
          googlevideo ya caducada, así que la uri buena sale del origen guardado.
          Volver a intentar con la misma URL muerta era exactamente el bug.

        Cualquier otra cosa —los clips del DJ, que no tienen origen guardado— pasa
        derecho. Y ante cualquier tropiezo se devuelve la original en vez de
        reventar: si mañana el plugin vuelve a funcionar, el camino viejo sigue ahí.

        Si el cog ya pidió `precargar()` de esta pista, primero se mira esa URL
        adelantada (ver `_tomar_precarga`); sin precarga, o si no sirve, todo
        sigue igual que siempre.
        """
        pagina = _pagina_de(pista)
        if not pagina:
            return pista

        pista_http = await self._tomar_precarga(pista)
        if pista_http is None:
            cargada = await self._cargar(pagina, titulo_de(pista))
            if cargada is None:
                return pista
            pista_http = cargada[0]

        # La cara sale de la pista de ahora y no de la que se precargó: entre una
        # y otra pudo cambiar (un reintento ya trae el origen guardado).
        return _con_la_cara_del_original(pista_http, pista)

    async def _cargar(
        self, pagina: str, titulo: str
    ) -> tuple[wavelink.Playable, float] | None:
        """De la página de YouTube a una pista http ya cargada en Lavalink.

        Devuelve la pista tal como la entrega Lavalink (sin la cara del original,
        que se le pone al usarla) y el momento en que vence su URL (epoch en
        segundos), o None si yt-dlp o Lavalink no pudieron. `titulo` es solo para
        los logs.
        """
        inicio = time.monotonic()
        url = await self.direct_url(pagina)
        if not url:
            log.warning("yt-dlp no entregó URL para %r", titulo)
            return None
        # La hora se toma al tener la URL y no al terminar de cargarla: si falta
        # `expire`, es desde aquí que corre la vida que se le supone.
        resuelta_a = time.time()
        log.info(
            "URL resuelta para %r en %.1fs (%s)",
            titulo, time.monotonic() - inicio, _resumen_url(url),
        )

        encontradas = None
        for intento in range(1, INTENTOS_DE_CARGA + 1):
            try:
                encontradas = await wavelink.Pool.fetch_tracks(url)
            except Exception as error:  # noqa: BLE001
                log.warning(
                    "Lavalink no pudo cargar la URL de %r (intento %s/%s): %s",
                    titulo, intento, INTENTOS_DE_CARGA, error,
                )
                encontradas = None
            else:
                if encontradas:
                    break
                log.warning(
                    "Lavalink no devolvió nada para la URL de %r (intento %s/%s)",
                    titulo, intento, INTENTOS_DE_CARGA,
                )
            if intento < INTENTOS_DE_CARGA:
                await asyncio.sleep(PAUSA_ENTRE_CARGAS)

        if not encontradas:
            return None

        vence = _vencimiento(url)
        if vence is None:
            vence = resuelta_a + VIDA_SIN_EXPIRE
        return encontradas[0], vence

    def precargar(self, pista: wavelink.Playable) -> None:
        """Adelanta en segundo plano la URL de una pista que va a sonar pronto.

        Es síncrona a propósito —el cog la llama desde un listener y no tiene que
        esperar nada—, pero necesita un loop en marcha. Las pistas sin página de
        YouTube (los clips del DJ) y los videos que ya se sabe que no suenan no se
        tocan, y pedir dos veces la misma no lanza dos yt-dlp.
        """
        pagina = _pagina_de(pista)
        clave = id_de_cancion(pista)
        if not pagina or not clave or clave in self._precargas:
            return
        if self.motivo_definitivo(pista):
            return

        titulo = titulo_de(pista)
        log.debug("Precargando la URL de %r", titulo)
        self._precargas[clave] = asyncio.create_task(self._precarga(pagina, titulo))
        while len(self._precargas) > PRECARGAS_MAXIMAS:
            _, vieja = self._precargas.popitem(last=False)
            # Si sigue en curso, no tiene sentido terminarla: nadie va a esperarla.
            vieja.cancel()

    async def _precarga(
        self, pagina: str, titulo: str
    ) -> tuple[wavelink.Playable, float] | None:
        """`_cargar` para correr suelta: nada de lo que pase escapa de la tarea.

        Nadie mira la tarea hasta que a la pista le toca sonar (y a lo mejor
        nunca), así que una excepción sin atrapar se perdería como "Task exception
        was never retrieved". Aquí queda en el log y la precarga cuenta como fallida.
        """
        try:
            return await self._cargar(pagina, titulo)
        except Exception:  # noqa: BLE001
            log.warning("Falló la precarga de %r", titulo, exc_info=True)
            return None

    async def _tomar_precarga(self, pista: wavelink.Playable) -> wavelink.Playable | None:
        """La pista http precargada para esta, si la hay y todavía sirve.

        Se saca de la tabla (`pop`) para que cada precarga se use una sola vez: en
        el reintento tras un fallo tiene que pedirse una URL nueva, no volver la
        misma. Si la tarea sigue en curso se espera en vez de lanzar otro yt-dlp
        en paralelo por la misma URL: es lo que pasa con un skip apurado.
        """
        tarea = self._precargas.pop(id_de_cancion(pista), None)
        if tarea is None:
            return None

        titulo = titulo_de(pista)
        espera = time.monotonic()
        cargada = await tarea
        espera = time.monotonic() - espera
        if cargada is None:
            log.info("La precarga de %r no dio nada: se resuelve de nuevo", titulo)
            return None

        pista_http, vence = cargada
        queda = vence - time.time()
        if queda <= _duracion_en_s(pista) + MARGEN_DE_VENCIMIENTO:
            log.info(
                "La URL precargada de %r no alcanza para toda la canción "
                "(le quedan %.0f min): se resuelve de nuevo", titulo, queda / 60,
            )
            return None

        log.info(
            "URL precargada para %r (esperé %.1f s, le quedan %.0f min)",
            titulo, espera, queda / 60,
        )
        return pista_http


class ResolvingPlayer(wavelink.Player):
    """El player de siempre, pero resolviendo el audio antes de tocarlo.

    Se engancha en `play()` y no en el comando `!play` porque quien va pasando
    de tema en tema es el AutoPlay de wavelink, que llama aquí adentro: haciéndolo
    en este punto quedan cubiertos los tres caminos —comando, botón del panel y
    autoplay— sin repetir la lógica en cada uno.
    """

    # Dónde se cortó cada canción, por id de YouTube, para que el reintento siga
    # desde ahí y no arranque de cero a mitad de tema.
    reanudar: dict[str, int] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # El del bot, para que el cog y todos los players vean lo mismo. Si el
        # bot no lo tiene, el cog lo pisa al conectar (`Music._connect_voice`).
        self.resolver = getattr(self.client, "resolver", None) or Resolver()
        self.reanudar = {}
        # Cuántos `play()` hay en curso. Mientras yt-dlp resuelve, `playing` da
        # falso (wavelink recién marca el tema al mandárselo a Lavalink) y quien
        # mirara solo eso largaría otro tema encima de este.
        self.resolviendo = 0

    async def play(self, track: wavelink.Playable, *args, **kwargs):
        inicio = self.reanudar.pop(id_de_cancion(track), 0)
        if inicio and not kwargs.get("start"):
            kwargs["start"] = inicio
        self.resolviendo += 1
        try:
            resuelta = await self.resolver.resolve(track)
            # Resolver tarda segundos y en ese rato pudo llegar un `!stop`, la
            # desconexión por inactividad o el corte por canal vacío. Si ahora
            # se llamara a `super().play()`, wavelink haría el PATCH igual y
            # Lavalink recrearía un player huérfano de un servidor ya cerrado.
            if not self.connected:
                log.debug("El player se desconectó mientras se resolvía %r: no toco", titulo_de(track))
                return None
            return await super().play(resuelta, *args, **kwargs)
        finally:
            self.resolviendo -= 1
