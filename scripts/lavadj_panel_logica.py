"""Lo que el panel de LavaDJ hace sin GTK: hablar con el bot, lanzarlo y armar textos.

Solo biblioteca estándar: el panel corre con el python del SISTEMA y esto se
prueba con el del venv (tests/test_panel_logica.py). Nada de `gi` aquí.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT / ".venv" / "bin" / "python"
BOT_SCRIPT = PROJECT / "bot.py"
LOG_FILE = PROJECT / "logs" / "bot.log"
START_LOG = PROJECT / "logs" / "arranque.log"
LAVALINK_SCRIPT = PROJECT / "scripts" / "lavalink" / "start-lavalink.sh"


def _dir_de_config() -> Path:
    """Lo mismo que GLib.get_user_config_dir(), sin GLib."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    return Path(xdg) if os.path.isabs(xdg) else Path.home() / ".config"


AJUSTES = _dir_de_config() / "lavadj" / "panel.json"
# Compat: antes del cambio de nombre los ajustes vivían en ~/.config/huffybot.
AJUSTES_VIEJOS = _dir_de_config() / "huffybot" / "panel.json"


def _del_env(nombre: str, por_defecto: str) -> str:
    """Una variable del .env del proyecto, leída a mano: aquí no hay python-dotenv.

    Sin esto, cambiar API_PORT en el .env dejaba al panel hablándole al puerto
    viejo y mostrando "FUERA DEL AIRE" con el bot perfectamente vivo.
    """
    try:
        for linea in (PROJECT / ".env").read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea.startswith("#") or "=" not in linea:
                continue
            clave, _, valor = linea.partition("=")
            if clave.strip() == nombre:
                return valor.strip().strip("'\"") or por_defecto
    except OSError:
        pass
    return por_defecto


API_BASE = f"http://127.0.0.1:{_del_env('API_PORT', '9876')}/api"
LAVALINK_URL = _del_env("LAVALINK_URI", "http://127.0.0.1:2333").rstrip("/") + "/version"
LAVALINK_PASSWORD = _del_env("LAVALINK_PASSWORD", "youshallnotpass")

REFRESCO = 2          # segundos entre lecturas de estado
LINEAS_MONITOR = 200  # cuántas líneas de log guarda el cajón
# Lo mismo que rota bot.log; arranque.log había llegado a 11 MB sin que nadie lo mirara.
ARRANQUE_MAXIMO = 2_000_000
# El bot se despide de los canales y baja Lavalink (hasta 30 s) antes de soltar la API.
ESPERA_DEL_CIERRE = 45


# --- Estado del bot --------------------------------------------------------

def estado_bot() -> dict | None:
    """Lo que dice la API. None = no hay bot escuchando."""
    try:
        with urllib.request.urlopen(f"{API_BASE}/status", timeout=0.5) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None


def consultar(ruta: str, metodo: str = "GET", timeout: float = 2.0, **params) -> tuple[str, dict]:
    """Llama a la API. Devuelve (error, datos); error vacío = salió bien."""
    url = f"{API_BASE}/{ruta}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    peticion = urllib.request.Request(url, method=metodo)
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as resp:
            cuerpo = resp.read().decode()
    except urllib.error.URLError as error:
        return str(getattr(error, "reason", error)), {}
    except (TimeoutError, OSError) as error:
        return str(error), {}
    try:
        return "", json.loads(cuerpo)
    except json.JSONDecodeError:
        return "", {}


def postear(ruta: str, timeout: float = 2.0, **params) -> str:
    """POST a la API. Devuelve "" si salió bien, o el motivo del fallo.

    `motivo` queda escrito en el log del bot: cuando la transmisión se corta
    sola, lo primero que uno quiere saber es quién lo pidió.
    """
    return consultar(ruta, "POST", timeout, **params)[0]


def lavalink_arriba() -> bool:
    """El nodo de audio. Sin él el bot levanta igual, pero no suena nada."""
    peticion = urllib.request.Request(
        LAVALINK_URL, headers={"Authorization": LAVALINK_PASSWORD}
    )
    try:
        with urllib.request.urlopen(peticion, timeout=0.5):
            return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _script_de_lavalink(accion: str, verbo: str, plazo: int) -> str:
    """Corre `start-lavalink.sh <accion>`. Devuelve "" si salió bien, o el motivo."""
    try:
        resultado = subprocess.run(
            [str(LAVALINK_SCRIPT), accion],
            capture_output=True, text=True, timeout=plazo,
        )
    except FileNotFoundError:
        return "falta start-lavalink.sh"
    except subprocess.TimeoutExpired:
        return f"Lavalink tardó demasiado en {verbo}"
    if resultado.returncode != 0:
        ultima = (resultado.stdout + resultado.stderr).strip().splitlines()
        return ultima[-1][:120] if ultima else f"no pude {verbo} Lavalink"
    return ""


def levantar_lavalink() -> str:
    """Arranca el contenedor de Lavalink. Bloquea: va en un hilo aparte."""
    return _script_de_lavalink("start", "levantar", 180)


def bajar_lavalink() -> str:
    """Baja el contenedor de Lavalink, lo simétrico de `levantar_lavalink`. Bloquea: va en un hilo aparte."""
    return _script_de_lavalink("stop", "bajar", 60)


def cortar_todo(motivo: str, espera: float = ESPERA_DEL_CIERRE) -> str:
    """Corta el bot y baja Lavalink. Devuelve "" si salió bien, o el motivo. Bloquea: va en un hilo aparte.

    El bot baja Lavalink él mismo (`todo=1`), después de despedirse de los canales
    de voz: con el nodo caído antes, los players no alcanzarían a salir. Aquí se
    espera a que suelte la API (lo último que cierra) y, si el nodo sigue arriba
    —un bot anterior a `todo`, o su script falló—, se baja desde el panel.
    """
    error = postear("stop", motivo=motivo, todo=1)
    if error:
        return error
    limite = time.monotonic() + espera
    while estado_bot() is not None:
        if time.monotonic() >= limite:
            return "el bot no cerró a tiempo"
        time.sleep(0.5)
    return bajar_lavalink() if lavalink_arriba() else ""


def rotar_arranque() -> None:
    """Corre arranque.log a .1 cuando se pasó de tamaño.

    Va ANTES de tomar la marca de posición: si se rotara después,
    motivo_de_la_caida() leería con un offset del archivo viejo.
    """
    try:
        if START_LOG.exists() and START_LOG.stat().st_size > ARRANQUE_MAXIMO:
            START_LOG.replace(START_LOG.parent / f"{START_LOG.name}.1")
    except OSError:
        pass  # que no rote no es motivo para no arrancar el bot


def levantar_bot() -> tuple[str, int]:
    """Lanza el bot. Devuelve (error, posición del log antes de arrancar).

    La posición sirve para leer después SOLO lo que escupió este arranque: si el
    proceso se cae por un import roto, el panel puede mostrar el motivo en vez
    de volver a "sin proceso" como si no hubiera pasado nada.
    """
    if not VENV_PYTHON.exists():
        return f"Falta el venv en {VENV_PYTHON.parent}", 0
    START_LOG.parent.mkdir(exist_ok=True)
    rotar_arranque()
    marca = START_LOG.stat().st_size if START_LOG.exists() else 0
    try:
        salida = open(START_LOG, "a")
    except OSError as error:
        return f"No se pudo escribir {START_LOG.name}: {error.strerror}", marca
    try:
        subprocess.Popen(
            [str(VENV_PYTHON), str(BOT_SCRIPT)],
            cwd=PROJECT,
            stdout=salida,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # sobrevive al cierre del panel
        )
    except OSError as error:
        return f"No arrancó: {error.strerror}", marca
    finally:
        salida.close()
    return "", marca


def motivo_de_la_caida(desde: int) -> str:
    """La última línea con pinta de error de lo que escribió este arranque."""
    try:
        with open(START_LOG, "r", errors="replace") as archivo:
            archivo.seek(desde)
            lineas = [l.strip() for l in archivo.read().splitlines() if l.strip()]
    except OSError:
        return ""
    if not lineas:
        return ""
    # El traceback termina en la línea que importa: "ModuleNotFoundError: ...".
    for linea in reversed(lineas):
        if not linea.startswith(("File ", "  ", "Traceback")):
            return linea[:160]
    return lineas[-1][:160]


def veredicto_de_arranque(vigilando: tuple[int, float] | None, estado: dict | None,
                          ahora: float) -> str:
    """Cómo va un arranque vigilado: "levanto", "vencio" o "" (aún no hay nada que hacer).

    `vigilando` es (posición del log, hasta cuándo esperamos), en el reloj de `ahora`.
    """
    if not vigilando:
        return ""
    if estado:
        return "levanto"
    return "vencio" if ahora >= vigilando[1] else ""


# --- Textos ----------------------------------------------------------------

def texto_edad(segundos: float | None) -> str:
    """Cuándo se bajaron los titulares. None = nunca."""
    if segundos is None:
        return "sin bajar"
    if segundos < 60:
        return "recién bajados"
    return f"hace {texto_duracion(segundos)}"


def texto_duracion(segundos: float) -> str:
    minutos = int(segundos // 60)
    if minutos < 1:
        return f"{int(segundos)} s"
    if minutos < 60:
        return f"{minutos} min"
    horas, resto = divmod(minutos, 60)
    return f"{horas} h {resto:02d} min"


def texto_de_estado(estado: dict | None, ocupado: bool = False, tarea: str = "",
                    vigilando: bool = False) -> str:
    """Qué está haciendo la emisora ahora. Nunca un mensaje de evento."""
    if ocupado:
        return tarea
    if vigilando:
        return "levantando…"
    if not estado:
        return "sin proceso"
    sonando = next(
        (s["cancion"] for s in estado.get("voz", []) if s.get("cancion")), None)
    if sonando:
        return f"♪ {sonando}"
    vivo = estado.get("uptime")
    desde = f" · hace {texto_duracion(vivo)}" if vivo is not None else ""
    return f"{estado.get('usuario') or 'conectado'}{desde}"


def aviso_de_salud(estado: dict | None) -> tuple[str, str, str] | None:
    """(texto, clase, detalle) del testigo de averías, o None si no hay nada que decir.

    Se informa lo más grave primero. Un bot de antes de que esto existiera no
    manda `salud` y el testigo simplemente queda apagado.
    """
    salud = (estado or {}).get("salud") or {}
    ytdlp = (estado or {}).get("ytdlp") or {}
    if salud.get("estado") == "mudo":
        return "MUDO", "grave", salud.get("motivo") or ""
    if salud.get("estado") == "flojo":
        return "FLOJO", "aviso", salud.get("motivo") or ""
    if ytdlp.get("viejo"):
        return "YT-DLP", "aviso", (f"yt-dlp está en {ytdlp.get('version')}, "
                                   f"hace {ytdlp.get('dias')} días. Conviene actualizarlo.")
    if ytdlp.get("cliente") and ytdlp.get("cliente") != ytdlp.get("cliente_configurado"):
        return "YT-DLP", "aviso", (
            f"El bot cambió solo el cliente de YouTube de "
            f"{ytdlp.get('cliente_configurado')} a {ytdlp.get('cliente')}. "
            f"Para fijarlo, pon YTDLP_PLAYER_CLIENT={ytdlp.get('cliente')} en el .env.")
    return None


def aviso_sin_titulares(datos: dict) -> str:
    """Por qué el cajón de titulares está vacío."""
    if not datos.get("fuentes"):
        return "No hay feeds configurados (dj_feeds.json)."
    return "El DJ todavía no bajó noticias. Prueba «Refrescar noticias»."


def resumen_de_titulares(datos: dict) -> str:
    """Cabecera del cajón: cuántos hay, cuántos ya salieron al aire y qué tan frescos."""
    titulares = datos.get("titulares") or []
    leidos = sum(1 for t in titulares if t.get("leido"))
    return (f"{len(titulares)} TITULARES · {leidos} YA AL AIRE"
            f" · {texto_edad(datos.get('edad'))}")


def botones_activos(al_aire: bool, ocupado: bool, vigilando: bool) -> tuple[bool, bool, bool]:
    """Si están activos (Salir al aire, Cortar, Refrescar noticias).

    Mientras se vigila el arranque el bot todavía no contesta (al_aire es falso):
    sin `vigilando` el primer botón se reactivaba y un segundo click lanzaba un
    segundo bot.
    """
    return (not al_aire and not ocupado and not vigilando,
            al_aire and not ocupado,
            al_aire and not ocupado)


def _acortar(linea: str) -> str:
    """"2026-08-03 00:14:13,831 INFO bot: Bot listo" -> "00:14  bot: Bot listo"."""
    partes = linea.split(None, 3)
    if len(partes) == 4 and partes[1][:2].isdigit():
        return f"{partes[1][:5]}  {partes[3]}"
    return linea


def lineas_de_log(cuantas: int = 1, fuente: Path = LOG_FILE) -> list[str]:
    try:
        with open(fuente, "rb") as archivo:
            archivo.seek(0, 2)
            archivo.seek(max(0, archivo.tell() - 16384))
            crudas = archivo.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    utiles = [_acortar(l) for l in crudas if l.strip()]
    return utiles[-cuantas:]


# --- Ajustes ---------------------------------------------------------------

def leer_ajustes() -> dict:
    for ruta in (AJUSTES, AJUSTES_VIEJOS):  # el viejo solo si aún no hay nuevo
        try:
            return json.loads(ruta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def guardar_ajuste(clave: str, valor) -> None:
    datos = leer_ajustes()
    datos[clave] = valor
    try:
        AJUSTES.parent.mkdir(parents=True, exist_ok=True)
        AJUSTES.write_text(json.dumps(datos))
    except OSError:
        pass  # que no se guarde la preferencia no es motivo para romper nada
