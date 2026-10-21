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

El título, el autor y la carátula que muestra el panel salen de la pista
original de YouTube: la fuente `http` no trae metadatos y sin esto todo se
vería como "Unknown title".
"""

import asyncio
import copy
import datetime
import logging
import time
from collections import OrderedDict
from urllib.parse import parse_qs, urlparse

import wavelink

from core import config

log = logging.getLogger("resolver")

# Sin esto yt-dlp usa sus clientes por defecto y se come el mismo "Sign in to
# confirm you're not a bot" que tumbó al plugin.
CLIENT_ARG = f"youtube:player_client={config.YTDLP_PLAYER_CLIENT}"

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

# Los videos que ya sabemos que no van a funcionar, por uri de YouTube. Acotada: es
# una ayuda para no quemar reintentos, no un registro histórico.
RECUERDO_MAXIMO = 100
_SIN_CASO: "OrderedDict[str, str]" = OrderedDict()


def _clasificar(error: str) -> str | None:
    """El motivo en castellano si el fallo es definitivo, o None si es pasajero."""
    plano = error.lower()
    for fragmento, motivo in MOTIVOS_DEFINITIVOS:
        if fragmento in plano:
            return motivo
    return None


def olvidar_motivos() -> None:
    """Vacía la tabla de videos sin caso. Al arrancar y en los tests."""
    _SIN_CASO.clear()


def motivo_definitivo(pista: wavelink.Playable) -> str | None:
    """Por qué esta canción no va a sonar nunca, si ya lo sabemos.

    Se busca por la uri de YouTube: en el reintento la pista llega con la de
    googlevideo, así que primero se mira el origen guardado.
    """
    uri = _origen(pista).get("uri") or getattr(pista, "uri", "") or ""
    return _SIN_CASO.get(uri)


async def direct_url(webpage_url: str) -> str | None:
    """La URL directa del audio, o None si yt-dlp no pudo."""
    if not webpage_url:
        return None

    # Nunca por shell: los títulos y las URLs vienen de YouTube y no son de fiar.
    proceso = await asyncio.create_subprocess_exec(
        config.YTDLP_BIN,
        "--no-warnings",
        "--skip-download",
        "--no-playlist",
        "--extractor-args", CLIENT_ARG,
        "-f", FORMATO,
        "--get-url",
        webpage_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        salida, error = await asyncio.wait_for(
            proceso.communicate(), timeout=config.YTDLP_TIMEOUT
        )
    except asyncio.TimeoutError:
        proceso.kill()
        await proceso.wait()
        log.warning("yt-dlp se pasó de %ss con %s", config.YTDLP_TIMEOUT, webpage_url)
        return None
    except FileNotFoundError:
        log.error("No encuentro yt-dlp en %s", config.YTDLP_BIN)
        return None

    if proceso.returncode != 0:
        detalle = error.decode(errors="replace").strip()[:200]
        log.warning("yt-dlp no pudo con %s: %s", webpage_url, detalle)
        motivo = _clasificar(detalle)
        if motivo:
            _SIN_CASO[webpage_url] = motivo
            _SIN_CASO.move_to_end(webpage_url)
            while len(_SIN_CASO) > RECUERDO_MAXIMO:
                _SIN_CASO.popitem(last=False)
        return None

    # `--get-url` imprime una línea por formato; con un formato pedido es una.
    for linea in salida.decode(errors="replace").splitlines():
        linea = linea.strip()
        if linea.startswith("http"):
            return linea
    return None


def _resumen_url(url: str) -> str:
    """Host, itag y caducidad de una URL de googlevideo, sin la firma."""
    consulta = parse_qs(urlparse(url).query)
    caduca = consulta.get("expire", ["?"])[0]
    return f"host={urlparse(url).hostname} itag={consulta.get('itag', ['?'])[0]} expire={caduca}"


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


# yt-dlp es el punto único de falla de toda la música y nadie lo mira. Su
# versión es una fecha, así que la antigüedad se calcula sin pedirle nada a la
# red: si quedó viejo, se ve en el panel.
DIAS_PARA_QUE_ESTE_VIEJO = 30

_ESTADO_YTDLP: dict = {"version": None, "dias": None, "viejo": False}


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


async def revisar_ytdlp() -> dict:
    """Le pregunta la versión a yt-dlp y la deja anotada para el panel."""
    version = None
    proceso = None
    try:
        proceso = await asyncio.create_subprocess_exec(
            config.YTDLP_BIN, "--version",
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
    _ESTADO_YTDLP.update(
        version=version,
        dias=dias,
        viejo=dias is not None and dias > DIAS_PARA_QUE_ESTE_VIEJO,
    )
    if _ESTADO_YTDLP["viejo"]:
        log.warning("yt-dlp está en %s, hace %s días. Conviene actualizarlo.", version, dias)
    return dict(_ESTADO_YTDLP)


def estado_ytdlp() -> dict:
    """Lo último que se supo de yt-dlp. Lo lee la API para el panel."""
    return dict(_ESTADO_YTDLP)


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


async def resolve(pista: wavelink.Playable) -> wavelink.Playable:
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
    """
    origen = _origen(pista)
    if getattr(pista, "source", None) == "youtube":
        pagina = pista.uri or ""
    else:
        pagina = origen.get("uri") or ""
    if not pagina:
        return pista

    inicio = time.monotonic()
    url = await direct_url(pagina)
    if not url:
        log.warning("yt-dlp no entregó URL para %r", titulo_de(pista))
        return pista
    log.info(
        "URL resuelta para %r en %.1fs (%s)",
        titulo_de(pista), time.monotonic() - inicio, _resumen_url(url),
    )

    encontradas = None
    for intento in range(1, INTENTOS_DE_CARGA + 1):
        try:
            encontradas = await wavelink.Pool.fetch_tracks(url)
        except Exception as error:  # noqa: BLE001
            log.warning(
                "Lavalink no pudo cargar la URL de %r (intento %s/%s): %s",
                titulo_de(pista), intento, INTENTOS_DE_CARGA, error,
            )
            encontradas = None
        else:
            if encontradas:
                break
            log.warning(
                "Lavalink no devolvió nada para la URL de %r (intento %s/%s)",
                titulo_de(pista), intento, INTENTOS_DE_CARGA,
            )
        if intento < INTENTOS_DE_CARGA:
            await asyncio.sleep(PAUSA_ENTRE_CARGAS)

    if not encontradas:
        return pista

    return _con_la_cara_del_original(encontradas[0], pista)


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
            return await super().play(await resolve(track), *args, **kwargs)
        finally:
            self.resolviendo -= 1
