#!/usr/bin/python3
"""Panel de control de LavaDJ: levantar, cortar y refrescar noticias.

Corre con el python del SISTEMA (necesita GTK4/libadwaita, que no están en el
venv del proyecto) y lanza el bot con el python del venv.

No sabe nada del proceso del bot: le habla por HTTP a la API que el propio bot
levanta en localhost (core/api.py). Lo único que hace "a la antigua" es el
arranque, que sí necesita lanzar un proceso.

El aparato tiene dos acabados —noche y día— y el testigo de aire es rojo en los
dos: una lámpara de ON AIR no cambia de color según la luz de la sala.
"""

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT / ".venv" / "bin" / "python"
BOT_SCRIPT = PROJECT / "bot.py"
LOG_FILE = PROJECT / "logs" / "bot.log"
START_LOG = PROJECT / "logs" / "arranque.log"
LAVALINK_SCRIPT = PROJECT / "scripts" / "lavalink" / "start-lavalink.sh"

AJUSTES = Path(GLib.get_user_config_dir()) / "lavadj" / "panel.json"
# Compat: antes del cambio de nombre los ajustes vivían en ~/.config/huffybot.
AJUSTES_VIEJOS = Path(GLib.get_user_config_dir()) / "huffybot" / "panel.json"


def _del_env(nombre: str, por_defecto: str) -> str:
    """Una variable del .env del proyecto.

    El bot las lee con python-dotenv desde core/config.py, pero aquí corre el
    python del sistema y no hay dotenv: para tres valores alcanza con leer el
    archivo. Sin esto, cambiar API_PORT en el .env dejaba al panel hablándole
    al puerto viejo y mostrando "FUERA DEL AIRE" con el bot perfectamente vivo.
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


def levantar_lavalink() -> str:
    """Arranca el contenedor de Lavalink. Bloquea: va en un hilo aparte."""
    try:
        resultado = subprocess.run(
            [str(LAVALINK_SCRIPT), "start"],
            capture_output=True, text=True, timeout=180,
        )
    except FileNotFoundError:
        return "falta start-lavalink.sh"
    except subprocess.TimeoutExpired:
        return "Lavalink tardó demasiado en levantar"
    if resultado.returncode != 0:
        ultima = (resultado.stdout + resultado.stderr).strip().splitlines()
        return ultima[-1][:120] if ultima else "no pude levantar Lavalink"
    return ""


# Lo mismo que rota bot.log. Sin esto el panel appendea para siempre: este
# archivo había llegado a 11 MB y nadie lo mira nunca.
ARRANQUE_MAXIMO = 2_000_000


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


# --- Interfaz --------------------------------------------------------------
#
# Dos acabados del mismo aparato. Lo único que NO cambia entre uno y otro es el
# testigo de aire: una lámpara de ON AIR es roja con la sala iluminada o a
# oscuras, y es el ancla de todo el panel.
#
# Todo lo de aquí abajo está scopeado bajo .panel a propósito. Un selector
# `button {}` pelado le pisaría el estilo a los botones de los toasts, a los
# diálogos y a cualquier cosa que libadwaita dibuje por su cuenta.

CSS = """
/* --- Estructura, común a los dos acabados --- */
.chasis { padding: 10px 24px 22px 24px; }

.legend {
  font-family: "Fira Code", monospace;
  font-size: 10px;
  font-weight: bold;
}

.testigo {
  min-width: 26px; min-height: 26px;
  border-radius: 50%;
}

.estado {
  font-family: Cantarell, sans-serif;
  font-size: 25px; font-weight: 800;
}

.detalle {
  font-family: "Fira Code", monospace;
  font-size: 11px;
}

.piloto {
  font-family: "Fira Code", monospace;
  font-size: 9px;
  font-weight: bold;
  padding: 2px 7px;
  border-radius: 3px;
}

.monitor {
  font-family: "Fira Code", monospace;
  font-size: 10px;
  padding: 9px 24px;
}

.panel button.tirador {
  min-height: 0;
  padding: 3px 7px;
  margin: 5px 18px 5px 0;
  background: none;
  border: none;
  box-shadow: none;
}

.cajon textview, .cajon textview text {
  font-family: "Fira Code", monospace;
  font-size: 10px;
  padding: 8px 24px;
}

/* Titulares en cola: los que el DJ ya leyó se apagan, no desaparecen. */
.titulares { padding: 8px 24px; }
.titular {
  font-family: "Fira Code", monospace;
  font-size: 10px;
}

/* Botón de aire: el único que se sale del estilo del sistema, porque el rojo
   de aire es la identidad del aparato. El de cortar usa .destructive-action de
   libadwaita, que ya trae los estados correctos. */
.panel button.aire {
  font-weight: bold;
  border-radius: 6px;
  min-height: 38px;
  background: linear-gradient(#C33018, #92200D);
  border: 1px solid #E0563A;
  color: #FFF1EA;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.14);
}
.panel button.aire:hover { background: linear-gradient(#DB3A1E, #A82710); }
.panel button.aire:active { background: #8C1E0C; box-shadow: inset 0 2px 5px rgba(0,0,0,.5); }
.panel button.aire:focus-visible { outline: 2px solid #FF8368; outline-offset: 2px; }
/* Ya está al aire: el botón queda claramente muerto, para que el único rojo
   vivo del panel sea el de cortar. */
.panel button.aire:disabled,
.panel button.aire:disabled:backdrop {
  background: none;
  border: 1px solid alpha(@window_fg_color, .12);
  color: alpha(@window_fg_color, .40);
  box-shadow: none;
}

/* ================= ACABADO NOCHE ================= */
window.panel.noche { background: linear-gradient(#191512, #100D0B); }
.panel.noche .legend { color: #8A7259; }
.panel.noche .testigo {
  background: radial-gradient(circle at 34% 30%, #6B3A2E, #331A15 70%);
  box-shadow: inset 0 1px 2px rgba(0,0,0,.9), 0 0 0 1px #241713;
}
.panel.noche .estado { color: #4E4038; }
.panel.noche .estado.al-aire { color: #F4E7DA; }
.panel.noche .detalle { color: #E8A93C; }
.panel.noche .detalle.apagado { color: #6E5B49; }
.panel.noche .piloto { color: #6E5B49; background: #141110; border: 1px solid #2E2621; }
.panel.noche .piloto.encendido { color: #9FD68B; border-color: #33512B; }
.panel.noche .piloto.aviso { color: #E8A93C; border-color: #8A5312; }
.panel.noche .piloto.grave { color: #FF6A4D; border-color: #92200D; }
.panel.noche .monitor { color: #6E5B49; background: #0C0A09; border-top: 1px solid #241E1A; }
.panel.noche button.tirador { color: #6E5B49; }
.panel.noche button.tirador:hover { background: #1A1512; color: #A98F6F; }
.panel.noche .cajon { background: #0C0A09; border-top: 1px solid #241E1A; }
.panel.noche .titular { color: #C2A98A; }
.panel.noche .titular.leido { color: #5A4A3C; }
.panel.noche .cajon textview, .panel.noche .cajon textview text {
  background: #0C0A09; color: #7E6A55;
}

/* ================= ACABADO DÍA =================
   Frente de aluminio arena con la serigrafía grabada en grafito, no un tema
   oscuro invertido: la referencia es una consola de estudio, no papel. */
window.panel.dia { background: linear-gradient(#EAE6DB, #D9D4C7); }
.panel.dia .legend { color: #6E5C46; }
.panel.dia .testigo {
  background: radial-gradient(circle at 34% 30%, #B9A08F, #8A7261 70%);
  box-shadow: inset 0 1px 3px rgba(0,0,0,.45), 0 0 0 1px #B3A895;
}
.panel.dia .estado { color: #9C907D; }
.panel.dia .estado.al-aire { color: #211B14; }
.panel.dia .detalle { color: #8A5312; }
.panel.dia .detalle.apagado { color: #8E8371; }
.panel.dia .piloto { color: #7A6E5C; background: #F1ECE2; border: 1px solid #C0B6A3; }
.panel.dia .piloto.encendido { color: #3D6B2E; border-color: #96B98A; }
.panel.dia .piloto.aviso { color: #8A5312; border-color: #C2A98A; }
.panel.dia .piloto.grave { color: #C33018; border-color: #E5B6B6; }
.panel.dia .monitor { color: #7A6E5C; background: #F1ECE2; border-top: 1px solid #C0B6A3; }
.panel.dia button.tirador { color: #7A6E5C; }
.panel.dia button.tirador:hover { background: #E3DCCF; color: #4A3E2E; }
.panel.dia .cajon { background: #F1ECE2; border-top: 1px solid #C0B6A3; }
.panel.dia .titular { color: #3B3226; }
.panel.dia .titular.leido { color: #9A8F7C; }
.panel.dia .cajon textview, .panel.dia .cajon textview text {
  background: #F1ECE2; color: #6B5F4E;
}
/* libadwaita 1.9 dibuja los botones destructivos teñidos, no en rojo sólido —
   y para este panel está bien, deja al testigo como el único rojo saturado.
   Lo único que hay que corregir es que sobre el frente arena ese rosa queda
   casi al mismo tono que la chapa y el botón pierde el borde. Se oscurece lo
   justo para que se despegue; sigue siendo teñido, no sólido. */
.panel.dia button.destructive-action:not(:disabled) {
  background: #EDC7C7;
  color: #8E1721;
}
.panel.dia button.destructive-action:not(:disabled):hover { background: #E5B6B6; }

/* ================= EL TESTIGO DE AIRE =================
   Va al final a propósito: es lo único que NO cambia con el acabado, y tiene
   que ganarle a la regla apagada de cada paleta. Empatan en especificidad, así
   que lo que decide es el orden — no hay que moverlo más arriba.
   Lo único que cambia entre día y noche es el halo: de día una lámpara
   encendida casi no tiñe la sala. */
.panel .testigo.al-aire {
  background: radial-gradient(circle at 34% 30%, #FFB9A2, #FF3B21 55%, #B81E0B);
  box-shadow: inset 0 1px 2px rgba(0,0,0,.35), 0 0 0 1px #FF6A4D,
              0 0 26px 8px rgba(255,59,33,.42), 0 0 60px 18px rgba(255,59,33,.20);
}
.panel.dia .testigo.al-aire {
  box-shadow: inset 0 1px 2px rgba(0,0,0,.30), 0 0 0 1px #D8442A,
              0 0 14px 3px rgba(255,59,33,.28);
}
.panel .testigo.al-aire.late { animation: latido 3.4s ease-in-out infinite; }
@keyframes latido {
  0%,100% { box-shadow: inset 0 1px 2px rgba(0,0,0,.35), 0 0 0 1px #FF6A4D,
                        0 0 26px 8px rgba(255,59,33,.42),
                        0 0 60px 18px rgba(255,59,33,.20); }
  50%     { box-shadow: inset 0 1px 2px rgba(0,0,0,.35), 0 0 0 1px #FF8368,
                        0 0 34px 11px rgba(255,59,33,.60),
                        0 0 74px 24px rgba(255,59,33,.28); }
}
/* El latido de día late igual, pero sin encender media sala. */
.panel.dia .testigo.al-aire.late { animation: latido-dia 3.4s ease-in-out infinite; }
@keyframes latido-dia {
  0%,100% { box-shadow: inset 0 1px 2px rgba(0,0,0,.30), 0 0 0 1px #D8442A,
                        0 0 14px 3px rgba(255,59,33,.28); }
  50%     { box-shadow: inset 0 1px 2px rgba(0,0,0,.30), 0 0 0 1px #F05335,
                        0 0 20px 5px rgba(255,59,33,.42); }
}
"""


class Panel(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LavaDJ")
        self.add_css_class("panel")
        self.set_default_size(430, 330)
        self.esta_al_aire = False
        self.ocupado = False       # hay una tarea larga en un hilo
        self.tarea = ""            # qué es esa tarea, para el detalle
        self.ticks = 0             # vueltas del refresco, para lo que no va cada vez
        self.vigilando = None      # (posición del log, hasta cuándo esperamos)
        self.foco_puesto_para = None   # para qué estado acomodamos el foco
        self.envio_volumen = None      # timeout pendiente para mandar el volumen
        self.volumen_puesto = False    # ya le pasamos el volumen a ESTE bot
        # Normalmente el cajón muestra el log del bot; si el bot se cayó al
        # arrancar, el traceback está en el otro archivo y ahí hay que mirar.
        self.fuente_log = LOG_FILE

        # Los avisos van a toasts; el estado se queda quieto en su etiqueta.
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        raiz = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toasts.set_child(raiz)

        barra = Adw.HeaderBar(css_classes=["flat"])
        barra.set_title_widget(self._legend("LAVADJ · TRANSMISOR"))
        self.piloto = Gtk.Label(label="AUDIO", css_classes=["piloto"],
                                valign=Gtk.Align.CENTER,
                                tooltip_text="Nodo Lavalink")
        barra.pack_start(self.piloto)

        # El testigo de averías. Como en cualquier aparato: apagado mientras
        # todo anda, encendido cuando hay algo que mirar. Existe porque el bot
        # puede estar vivo y contestando y aun así no estar sonando nada.
        self.pulso = Gtk.Label(label="", css_classes=["piloto"],
                               valign=Gtk.Align.CENTER, visible=False)
        barra.pack_start(self.pulso)

        # La luz del panel: en un aparato esto es un dimmer, no un "tema".
        self.boton_luz = Gtk.Button(css_classes=["flat"])
        self.boton_luz.connect("clicked", self.cambiar_luz)
        barra.pack_end(self.boton_luz)
        raiz.append(barra)

        chasis = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            css_classes=["chasis"], vexpand=True,
        )
        # Sin esto, al estirar la ventana los botones se desparraman a lo ancho.
        marco = Adw.Clamp(maximum_size=420, tightening_threshold=380, child=chasis)
        raiz.append(marco)

        # Estado: testigo + palabra + detalle.
        fila = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.testigo = Gtk.Box(css_classes=["testigo"], valign=Gtk.Align.CENTER)
        fila.append(self.testigo)
        columna = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.estado = Gtk.Label(xalign=0, css_classes=["estado"])
        self.detalle = Gtk.Label(xalign=0, css_classes=["detalle"], ellipsize=3)
        columna.append(self.estado)
        columna.append(self.detalle)
        fila.append(columna)
        chasis.append(fila)

        # Controles.
        controles = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
            homogeneous=True, vexpand=True, valign=Gtk.Align.CENTER,
        )
        self.boton_aire = Gtk.Button(css_classes=["aire"])
        self.boton_aire.set_child(Adw.ButtonContent(
            icon_name="media-record-symbolic", label="Salir al aire"))
        self.boton_aire.connect("clicked", self.al_aire)
        self.boton_cortar = Gtk.Button(css_classes=["destructive-action"])
        self.boton_cortar.set_child(Adw.ButtonContent(
            icon_name="media-playback-stop-symbolic", label="Cortar"))
        self.boton_cortar.connect("clicked", self.cortar_transmision)
        controles.append(self.boton_aire)
        controles.append(self.boton_cortar)
        chasis.append(controles)

        self.boton_noticias = Gtk.Button(
            tooltip_text="Baja los titulares de nuevo y los muestra aquí abajo",
        )
        self.boton_noticias.set_child(Adw.ButtonContent(
            icon_name="view-refresh-symbolic", label="Refrescar noticias"))
        self.boton_noticias.connect("clicked", self.refrescar_noticias)
        chasis.append(self.boton_noticias)

        # Volumen de la voz del DJ: un dial más del aparato, no un ajuste de app.
        chasis.append(self._control_de_voz())

        # Monitor: una línea siempre visible y el resto en un cajón que se abre.
        self.monitor = Gtk.Label(xalign=0, css_classes=["monitor"], ellipsize=3)
        raiz.append(self.monitor)

        tiradores = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                            spacing=2, halign=Gtk.Align.END)
        self.tirador_titulares = Gtk.Button(
            css_classes=["tirador", "flat"],
            tooltip_text="Ver los titulares que el DJ tiene en cola")
        self.tirador_titulares.set_child(Adw.ButtonContent(
            icon_name="pan-down-symbolic", label="Titulares"))
        self.tirador_titulares.connect("clicked", self.alternar_titulares)
        tiradores.append(self.tirador_titulares)

        self.tirador = Gtk.Button(css_classes=["tirador", "flat"],
                                  tooltip_text="Ver el log")
        self.tirador.set_child(Adw.ButtonContent(
            icon_name="pan-down-symbolic", label="Log"))
        self.tirador.connect("clicked", self.alternar_cajon)
        tiradores.append(self.tirador)
        raiz.append(tiradores)

        # Cajón de titulares: qué noticias tiene cargadas el DJ esperando salir.
        self.lista_titulares = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4,
            css_classes=["titulares"],
        )
        vitrina = Gtk.ScrolledWindow(
            css_classes=["cajon"], min_content_height=190,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        vitrina.set_child(self.lista_titulares)
        self.cajon_titulares = Gtk.Revealer(
            child=vitrina, transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            transition_duration=180,
        )
        raiz.append(self.cajon_titulares)

        self.vista_log = Gtk.TextView(
            editable=False, cursor_visible=False, monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        visor = Gtk.ScrolledWindow(
            css_classes=["cajon"], min_content_height=190,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        visor.set_child(self.vista_log)
        self.cajon = Gtk.Revealer(
            child=visor, transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            transition_duration=180,
        )
        raiz.append(self.cajon)

        self.actualizar()
        GLib.timeout_add_seconds(REFRESCO, self.actualizar)

    def _control_de_voz(self) -> Gtk.Widget:
        """Fader del volumen de la voz del DJ: leyenda, corredera y lectura."""
        fila = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        fila.append(self._legend("VOZ DJ"))

        self.volumen = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 5)
        self.volumen.set_hexpand(True)
        self.volumen.set_draw_value(False)   # el número lo pone la lectura de al lado
        self.volumen.add_mark(100, Gtk.PositionType.BOTTOM, None)   # el nivel de fábrica
        self.volumen.set_value(leer_ajustes().get("dj_volumen", 100))
        self.volumen.set_tooltip_text(
            "Volumen de la voz del DJ (0 = mudo).\n"
            "El clip se prepara con la canción anterior: se escucha desde el próximo corte."
        )
        self.volumen.connect("value-changed", self.volumen_movido)
        fila.append(self.volumen)

        self.eco_volumen = Gtk.Label(css_classes=["detalle"], width_chars=4, xalign=1)
        fila.append(self.eco_volumen)
        self._mostrar_volumen()
        return fila

    def _legend(self, texto: str) -> Gtk.Label:
        etiqueta = Gtk.Label(css_classes=["legend"])
        etiqueta.set_markup(f'<span letter_spacing="2200">{texto}</span>')
        return etiqueta

    # --- Luz del panel ---
    def aplicar_luz(self, oscuro: bool) -> None:
        self.remove_css_class("dia")
        self.remove_css_class("noche")
        self.add_css_class("noche" if oscuro else "dia")
        # El icono muestra a qué se cambia, no en qué se está.
        self.boton_luz.set_icon_name(
            "weather-clear-symbolic" if oscuro else "weather-clear-night-symbolic")
        self.boton_luz.set_tooltip_text(
            "Encender la luz del panel" if oscuro else "Apagar la luz del panel")

    def cambiar_luz(self, _boton):
        gestor = Adw.StyleManager.get_default()
        oscuro = not gestor.get_dark()
        gestor.set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if oscuro else Adw.ColorScheme.FORCE_LIGHT)
        guardar_ajuste("luz", "noche" if oscuro else "dia")

    # --- Avisos ---
    def avisar(self, texto: str, *, falla: bool = False, con_log: bool = False) -> None:
        """Un toast por evento. El estado del aparato NO se toca aquí."""
        toast = Adw.Toast(title=texto)
        # Un fallo de arranque no debería evaporarse solo: se cierra a mano.
        toast.set_timeout(0 if falla else 4)
        if con_log:
            toast.set_button_label("Ver log")
            toast.connect("button-clicked", lambda *_: self.abrir_cajon(True))
        self.toasts.add_toast(toast)

    def en_hilo(self, trabajo, luego, que: str = "levantando el nodo de audio…") -> None:
        """Corre algo lento sin congelar la ventana. `que` es lo que se muestra."""
        self.ocupado = True
        self.tarea = que

        def correr():
            resultado = trabajo()
            GLib.idle_add(terminar, resultado)

        def terminar(resultado):
            self.ocupado = False
            luego(resultado)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=correr, daemon=True).start()
        self.actualizar()

    # --- Acciones ---
    def abrir_cajon(self, abierto: bool) -> None:
        self.cajon.set_reveal_child(abierto)
        self.tirador.get_child().set_icon_name(
            "pan-up-symbolic" if abierto else "pan-down-symbolic")
        self.tirador.set_tooltip_text("Ocultar el log" if abierto else "Ver el log")
        if abierto:
            # Los dos cajones salen del mismo borde: abiertos a la vez, la
            # ventana se estira al doble y no se ve bien ninguno.
            self.abrir_titulares(False)
            self.volcar_log()

    def alternar_cajon(self, _boton):
        self.abrir_cajon(not self.cajon.get_reveal_child())

    # --- Titulares del DJ ---
    def abrir_titulares(self, abierto: bool, recargar: bool = True) -> None:
        self.cajon_titulares.set_reveal_child(abierto)
        self.tirador_titulares.get_child().set_icon_name(
            "pan-up-symbolic" if abierto else "pan-down-symbolic")
        self.tirador_titulares.set_tooltip_text(
            "Ocultar los titulares" if abierto
            else "Ver los titulares que el DJ tiene en cola")
        if abierto:
            self.abrir_cajon(False)
            if recargar:
                self.cargar_titulares()

    def alternar_titulares(self, _boton):
        self.abrir_titulares(not self.cajon_titulares.get_reveal_child())

    def cargar_titulares(self) -> None:
        if not self.esta_al_aire:
            return self.pintar_titulares(None, "El bot está fuera del aire.")
        error, datos = consultar("headlines", timeout=1.5)
        if error:
            return self.pintar_titulares(None, f"No pude preguntarle al bot: {error}")
        self.pintar_titulares(datos)

    def pintar_titulares(self, datos: dict | None, aviso: str = "") -> None:
        """Rehace la lista del cajón. `datos` es lo que devolvió la API."""
        while (viejo := self.lista_titulares.get_first_child()) is not None:
            self.lista_titulares.remove(viejo)

        if datos is None:
            self.lista_titulares.append(
                Gtk.Label(label=aviso, xalign=0, css_classes=["titular", "leido"]))
            return

        titulares = datos.get("titulares") or []
        if not titulares:
            sin_fuentes = not datos.get("fuentes")
            self.lista_titulares.append(Gtk.Label(
                label=("No hay feeds configurados (dj_feeds.json)." if sin_fuentes
                       else "El DJ todavía no bajó noticias. Prueba «Refrescar noticias»."),
                xalign=0, wrap=True, css_classes=["titular", "leido"]))
            return

        leidos = sum(1 for t in titulares if t.get("leido"))
        self.lista_titulares.append(self._legend(
            f"{len(titulares)} TITULARES · {leidos} YA AL AIRE"
            f" · {texto_edad(datos.get('edad'))}"))
        for titular in titulares:
            ya = bool(titular.get("leido"))
            fila = Gtk.Label(
                label=f"{'✓' if ya else '•'}  {titular.get('texto', '')}",
                xalign=0, wrap=True,
                css_classes=["titular", "leido"] if ya else ["titular"],
            )
            self.lista_titulares.append(fila)

    # --- Volumen del DJ ---
    def _mostrar_volumen(self) -> None:
        valor = int(self.volumen.get_value())
        self.eco_volumen.set_text("MUDO" if valor == 0 else f"{valor}%")

    def volumen_movido(self, _escala):
        """Espera a que la mano se quede quieta antes de molestar al bot."""
        self._mostrar_volumen()
        if self.envio_volumen:
            GLib.source_remove(self.envio_volumen)
        self.envio_volumen = GLib.timeout_add(400, self.mandar_volumen)

    def mandar_volumen(self, avisar_fallos: bool = True):
        self.envio_volumen = None
        valor = int(self.volumen.get_value())
        guardar_ajuste("dj_volumen", valor)
        if self.esta_al_aire:
            error = postear("dj_volume", valor=valor)
            # El envío automático al levantar el bot no molesta con un toast:
            # nadie tocó nada, y un bot viejo sin ese endpoint daría error solo.
            if error and avisar_fallos:
                self.avisar(f"No pude mover el volumen del DJ: {error}", falla=True)
        return GLib.SOURCE_REMOVE

    def al_aire(self, _boton):
        # Se desactiva ya mismo, no cuando el bot conteste: dos clicks seguidos
        # lanzarían dos procesos y el segundo se cae peleando por el puerto.
        self.boton_aire.set_sensitive(False)
        # Sin nodo de audio el bot levanta pero no suena nada, así que el botón
        # se encarga de los dos: primero Lavalink, después el bot.
        if lavalink_arriba():
            self.arrancar_el_bot()
            return
        self.en_hilo(levantar_lavalink, self.tras_lavalink)

    def tras_lavalink(self, error: str):
        if error:
            self.avisar(f"El nodo de audio no levantó: {error}", falla=True)
            return
        self.arrancar_el_bot()

    def arrancar_el_bot(self):
        error, marca = levantar_bot()
        if error:
            self.avisar(error, falla=True, con_log=True)
            return
        # Si en 20 s no contesta la API, algo lo mató: hay que decir qué.
        self.vigilando = (marca, time.monotonic() + 20)

    def cortar_transmision(self, _boton):
        if not self.esta_al_aire:
            return
        self.vigilando = None
        error = postear("stop", motivo="boton Cortar del panel")
        if error:
            self.avisar(f"No pude cortar: {error}", falla=True)
        else:
            self.avisar("Transmisión cortada")

    def refrescar_noticias(self, _boton):
        if not self.esta_al_aire:
            return
        # Ahora la bajada ocurre aquí mismo (varios feeds con su timeout), así
        # que va en un hilo: si no, la ventana se queda dura unos segundos.
        self.en_hilo(
            lambda: consultar("refresh_news", "POST", timeout=20),
            self.tras_refrescar,
            que="bajando titulares…",
        )

    def tras_refrescar(self, resultado):
        error, datos = resultado
        if error:
            self.avisar(f"No pude refrescar: {error}", falla=True)
            return
        self.abrir_titulares(True, recargar=False)
        self.pintar_titulares(datos)
        cuantos = len(datos.get("titulares") or [])
        self.avisar(f"{cuantos} titulares en cola" if cuantos
                    else "Ningún feed devolvió titulares")

    # --- Estado ---
    def volcar_log(self):
        texto = "\n".join(lineas_de_log(LINEAS_MONITOR, self.fuente_log)) or "Sin log todavía"
        buffer = self.vista_log.get_buffer()
        if buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == texto:
            return
        buffer.set_text(texto)
        # Pegado abajo, como cualquier consola.
        self.vista_log.scroll_to_iter(buffer.get_end_iter(), 0, False, 0, 0)

    def revisar_arranque(self, estado: dict | None) -> None:
        """Vigila el arranque para no dejar al bot muriendo en silencio."""
        if not self.vigilando:
            return
        marca, plazo = self.vigilando
        if estado:                      # levantó bien
            self.vigilando = None
            self.fuente_log = LOG_FILE
            return
        if time.monotonic() < plazo:
            return
        self.vigilando = None
        # El traceback del arranque no pasa por bot.log: el cajón tiene que
        # apuntar al archivo donde sí está, o "Ver log" no muestra nada útil.
        self.fuente_log = START_LOG
        motivo = motivo_de_la_caida(marca)
        self.avisar(motivo or "El bot no levantó", falla=True, con_log=True)

    def texto_de_estado(self, estado: dict | None) -> str:
        """Qué está haciendo la emisora ahora. Nunca un mensaje de evento."""
        if self.ocupado:
            return self.tarea
        if self.vigilando:
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

    def pintar_salud(self, estado: dict | None) -> None:
        """Enciende el testigo de averías, o lo apaga si no hay nada que decir.

        Se informa lo más grave primero. Un bot de antes de que esto existiera
        no manda `salud` y el testigo simplemente queda apagado.
        """
        salud = (estado or {}).get("salud") or {}
        ytdlp = (estado or {}).get("ytdlp") or {}

        if salud.get("estado") == "mudo":
            texto, clase, detalle = "MUDO", "grave", salud.get("motivo") or ""
        elif salud.get("estado") == "flojo":
            texto, clase, detalle = "FLOJO", "aviso", salud.get("motivo") or ""
        elif ytdlp.get("viejo"):
            texto, clase = "YT-DLP", "aviso"
            detalle = (f"yt-dlp está en {ytdlp.get('version')}, "
                       f"hace {ytdlp.get('dias')} días. Conviene actualizarlo.")
        else:
            self.pulso.set_visible(False)
            return

        self.pulso.set_text(texto)
        self.pulso.set_css_classes(["piloto", clase])
        self.pulso.set_tooltip_text(detalle)
        self.pulso.set_visible(True)

    def actualizar(self, *_):
        estado = estado_bot()
        self.esta_al_aire = estado is not None
        al_aire = self.esta_al_aire
        self.ticks += 1
        self.revisar_arranque(estado)

        # El bot arranca con el volumen del .env y no sabe dónde quedó el fader;
        # la posición del panel manda, así que se la pasamos apenas levanta.
        if al_aire and not self.volumen_puesto:
            self.volumen_puesto = True
            self.mandar_volumen(avisar_fallos=False)
        elif not al_aire:
            self.volumen_puesto = False

        for widget in (self.testigo, self.estado):
            if al_aire:
                widget.add_css_class("al-aire")
            else:
                widget.remove_css_class("al-aire")
        # El latido solo si el sistema quiere animaciones: es una luz que pulsa
        # en un panel que puede quedar abierto horas.
        if al_aire and Gtk.Settings.get_default().get_property("gtk-enable-animations"):
            self.testigo.add_css_class("late")
        else:
            self.testigo.remove_css_class("late")

        self.estado.set_text("AL AIRE" if al_aire else "FUERA DEL AIRE")

        # El piloto de audio: si el bot está arriba se lo preguntamos a él, que
        # sabe si llegó a enganchar el nodo; si no, miramos el nodo directo.
        if estado:
            audio = bool(estado.get("lavalink", {}).get("conectado"))
        else:
            audio = lavalink_arriba()
        if audio:
            self.piloto.add_css_class("encendido")
        else:
            self.piloto.remove_css_class("encendido")
        self.piloto.set_tooltip_text(
            "Nodo Lavalink conectado" if audio
            else "Sin nodo Lavalink: no va a sonar nada")

        self.pintar_salud(estado)

        self.detalle.set_css_classes(["detalle"] if al_aire else ["detalle", "apagado"])
        self.detalle.set_text(self.texto_de_estado(estado))

        ocupado = self.ocupado
        # Mientras se vigila el arranque el bot todavía no contesta (al_aire es
        # falso): sin `vigilando` el botón se reactivaba y un segundo click
        # lanzaba un segundo bot. `revisar_arranque` lo limpia al levantar o al
        # vencer el plazo, así que no se queda apagado para siempre.
        self.boton_aire.set_sensitive(not al_aire and not ocupado and not self.vigilando)
        self.boton_cortar.set_sensitive(al_aire and not ocupado)
        self.boton_noticias.set_sensitive(al_aire and not ocupado)

        # Cuando el bot levanta, "Salir al aire" se apaga y GTK le pasa el foco
        # al botón de al lado, que es CORTAR: el siguiente Espacio o Enter te
        # tumbaba la transmisión sin que la tocaras. El foco se manda a mano a
        # un botón que no hace daño.
        if al_aire is not self.foco_puesto_para:
            self.foco_puesto_para = al_aire
            self.set_focus(self.boton_noticias if al_aire else self.boton_aire)

        ultima = lineas_de_log(1, self.fuente_log)
        self.monitor.set_text(ultima[0] if ultima else "Sin log todavía")
        if self.cajon.get_reveal_child():
            self.volcar_log()
        # Los titulares cambian de a poco (uno por corte del DJ): rehacer la
        # lista en cada vuelta sería una consulta HTTP cada dos segundos al pedo.
        if self.cajon_titulares.get_reveal_child() and self.ticks % 5 == 0:
            self.cargar_titulares()
        return GLib.SOURCE_CONTINUE


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.huffy_it.lavadj.panel")

    def do_activate(self):
        gestor = Adw.StyleManager.get_default()
        # Sin preferencia guardada seguimos al sistema; en cuanto se toca el
        # botón, manda la elección.
        guardado = leer_ajustes().get("luz")
        if guardado == "noche":
            gestor.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        elif guardado == "dia":
            gestor.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

        proveedor = Gtk.CssProvider()
        proveedor.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), proveedor,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        ventana = Panel(self)
        ventana.aplicar_luz(gestor.get_dark())
        gestor.connect("notify::dark",
                       lambda g, _p: ventana.aplicar_luz(g.get_dark()))
        ventana.present()


if __name__ == "__main__":
    App().run(None)
