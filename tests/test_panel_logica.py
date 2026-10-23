"""La lógica del panel sin GTK (`scripts/lavadj_panel_logica.py`): API del bot y Lavalink de juguete en 127.0.0.1, procesos doblados.

La ventana (`scripts/lavadj-panel.py`) no se importa aquí (sin GTK, y con guion en el nombre): solo se le comprueba que no use nombres sin definir.
"""

import ast
import builtins
import importlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def logica(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("lavadj_panel_logica")


def _escribir(ruta, texto):
    """Bytes crudos: en Windows `write_text` cambiaría los saltos de línea."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(texto.encode("utf-8"))
    return ruta


@pytest.mark.parametrize("segundos, esperado", [
    (0, "0 s"), (5.9, "5 s"), (59.9, "59 s"),
    (60, "1 min"), (125, "2 min"), (3599, "59 min"),
    (3600, "1 h 00 min"), (3660, "1 h 01 min"), (3725, "1 h 02 min"),
    (86400, "24 h 00 min"),
])
def test_texto_duracion(logica, segundos, esperado):
    assert logica.texto_duracion(segundos) == esperado


@pytest.mark.parametrize("segundos, esperado", [
    (None, "sin bajar"),
    (0, "recién bajados"), (59, "recién bajados"),
    (60, "hace 1 min"), (3599, "hace 59 min"), (3600, "hace 1 h 00 min"),
])
def test_texto_edad(logica, segundos, esperado):
    assert logica.texto_edad(segundos) == esperado


@pytest.mark.parametrize("linea, esperado", [
    ("2026-08-03 00:14:13,831 INFO bot: Bot listo", "00:14  bot: Bot listo"),
    ("2026-08-03 23:59:59,000 WARNING core.salud: algo  con espacios",
     "23:59  core.salud: algo  con espacios"),
    # Lo que no tiene pinta de línea de log se deja tal cual (tracebacks, texto suelto).
    ("Traceback (most recent call last):", "Traceback (most recent call last):"),
    ('  File "bot.py", line 3, in <module>', '  File "bot.py", line 3, in <module>'),
    ("texto suelto", "texto suelto"),
    ("", ""),
])
def test_acortar(logica, linea, esperado):
    assert logica._acortar(linea) == esperado


def test_estado_ocupado_muestra_la_tarea_aunque_no_haya_bot(logica):
    assert logica.texto_de_estado(None, ocupado=True, tarea="bajando titulares…") == "bajando titulares…"
    assert logica.texto_de_estado({"usuario": "x"}, ocupado=True, tarea="t", vigilando=True) == "t"


@pytest.mark.parametrize("estado, kwargs, esperado", [
    (None, {"vigilando": True}, "levantando…"),
    (None, {}, "sin proceso"),
    ({}, {}, "sin proceso"),
    ({"usuario": "LavaDJ", "uptime": 90, "voz": [{"cancion": None}, {"cancion": "Tema"}, {"cancion": "Otro"}]}, {}, "♪ Tema"),
    ({"usuario": "LavaDJ#1", "uptime": 125, "voz": [{"cancion": ""}]}, {}, "LavaDJ#1 · hace 2 min"),
    ({"usuario": "LavaDJ#1", "uptime": 0}, {}, "LavaDJ#1 · hace 0 s"),
    ({"usuario": "LavaDJ#1"}, {}, "LavaDJ#1"),
    ({"uptime": 60}, {}, "conectado · hace 1 min"),
    ({"usuario": None, "uptime": None}, {}, "conectado"),
])
def test_texto_de_estado(logica, estado, kwargs, esperado):
    assert logica.texto_de_estado(estado, **kwargs) == esperado


@pytest.mark.parametrize("estado", [
    None, {}, {"salud": None, "ytdlp": None}, {"salud": {"estado": "ok"}},
    {"ytdlp": {"viejo": False, "cliente": "android", "cliente_configurado": "android"}},
    {"ytdlp": {"cliente": None, "cliente_configurado": "android"}},
])
def test_salud_sin_novedades_apaga_el_testigo(logica, estado):
    assert logica.aviso_de_salud(estado) is None


@pytest.mark.parametrize("estado, esperado", [
    ({"salud": {"estado": "mudo", "motivo": "sin audio"}}, ("MUDO", "grave", "sin audio")),
    ({"salud": {"estado": "flojo", "motivo": "poca cola"}}, ("FLOJO", "aviso", "poca cola")),
    ({"salud": {"estado": "mudo"}}, ("MUDO", "grave", "")),
])
def test_salud_mudo_es_grave_y_flojo_es_aviso(logica, estado, esperado):
    assert logica.aviso_de_salud(estado) == esperado


def test_salud_yt_dlp_viejo_y_cliente_cambiado(logica):
    texto, clase, detalle = logica.aviso_de_salud({"ytdlp": {"viejo": True, "version": "2026.01.02", "dias": 45}})
    assert (texto, clase) == ("YT-DLP", "aviso")
    assert "2026.01.02" in detalle and "45 días" in detalle
    texto, clase, detalle = logica.aviso_de_salud({"ytdlp": {"cliente": "ios", "cliente_configurado": "android"}})
    assert (texto, clase) == ("YT-DLP", "aviso")
    assert "android" in detalle and "YTDLP_PLAYER_CLIENT=ios" in detalle


def test_salud_informa_primero_lo_mas_grave(logica):
    ytdlp = {"viejo": True, "version": "v", "dias": 40, "cliente": "ios", "cliente_configurado": "android"}
    assert logica.aviso_de_salud({"salud": {"estado": "mudo"}, "ytdlp": ytdlp})[0] == "MUDO"
    assert logica.aviso_de_salud({"salud": {"estado": "flojo"}, "ytdlp": ytdlp})[0] == "FLOJO"
    assert "días" in logica.aviso_de_salud({"ytdlp": ytdlp})[2]    # yt-dlp viejo gana al cambio de cliente


def test_aviso_sin_titulares_distingue_sin_feeds_de_sin_bajar(logica):
    assert "No hay feeds" in logica.aviso_sin_titulares({})
    assert "No hay feeds" in logica.aviso_sin_titulares({"fuentes": []})
    assert "todavía no bajó" in logica.aviso_sin_titulares({"fuentes": ["a"]})


def test_resumen_de_titulares(logica):
    datos = {"titulares": [{"leido": True}, {"leido": False}, {}], "edad": 120}
    assert logica.resumen_de_titulares(datos) == "3 TITULARES · 1 YA AL AIRE · hace 2 min"
    assert logica.resumen_de_titulares({"titulares": [{"leido": True}]}) == "1 TITULARES · 1 YA AL AIRE · sin bajar"


@pytest.mark.parametrize("al_aire, ocupado, vigilando, esperado", [
    (False, False, False, (True, False, False)),
    (True, False, False, (False, True, True)),
    # Una tarea larga en un hilo apaga todo.
    (False, True, False, (False, False, False)),
    (True, True, False, (False, False, False)),
    # Vigilando el arranque el bot aún no contesta: reactivar "Salir al aire" lanzaría dos.
    (False, False, True, (False, False, False)),
])
def test_botones_activos(logica, al_aire, ocupado, vigilando, esperado):
    assert logica.botones_activos(al_aire, ocupado, vigilando) == esperado


def test_veredicto_de_arranque(logica):
    vigilando = (100, 50.0)   # (posición del log, plazo)
    assert logica.veredicto_de_arranque(None, {"ok": 1}, 99.0) == ""
    assert logica.veredicto_de_arranque(vigilando, None, 49.9) == ""
    assert logica.veredicto_de_arranque(vigilando, None, 50.0) == "vencio"
    assert logica.veredicto_de_arranque(vigilando, None, 80.0) == "vencio"
    assert logica.veredicto_de_arranque(vigilando, {"usuario": "x"}, 10.0) == "levanto"
    # Si contestó, da igual que el plazo ya hubiera vencido.
    assert logica.veredicto_de_arranque(vigilando, {"usuario": "x"}, 80.0) == "levanto"


def test_del_env_y_sin_archivo(logica, monkeypatch, tmp_path):
    _escribir(tmp_path / ".env", "\n".join([
        "# API_PORT=1111",
        "  API_PORT = '1234' ",
        'LAVALINK_URI="http://127.0.0.1:2444/"',
        "TOKEN=abc=def==",
        "VACIA=",
        "SIN_IGUAL",
    ]))
    monkeypatch.setattr(logica, "PROJECT", tmp_path)
    assert logica._del_env("API_PORT", "9876") == "1234"
    assert logica._del_env("LAVALINK_URI", "x") == "http://127.0.0.1:2444/"
    assert logica._del_env("TOKEN", "x") == "abc=def=="
    assert logica._del_env("VACIA", "por defecto") == "por defecto"
    assert logica._del_env("SIN_IGUAL", "por defecto") == "por defecto"
    assert logica._del_env("NO_ESTA", "por defecto") == "por defecto"
    (tmp_path / ".env").unlink()
    assert logica._del_env("API_PORT", "9876") == "9876"    # sin archivo, el valor por defecto


def test_las_rutas_cuelgan_de_la_raiz_del_proyecto(logica):
    raiz = SCRIPTS.parent
    assert logica.PROJECT == raiz
    assert logica.BOT_SCRIPT == raiz / "bot.py"
    assert logica.LOG_FILE == raiz / "logs" / "bot.log"
    assert logica.START_LOG == raiz / "logs" / "arranque.log"
    assert logica.LAVALINK_SCRIPT == raiz / "scripts" / "lavalink" / "start-lavalink.sh"


def test_dir_de_config_sigue_xdg_config_home(logica, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert logica._dir_de_config() == tmp_path
    # Vacía o relativa no vale (así lo trata GLib): se cae a ~/.config.
    for invalida in ("", "relativa/config"):
        monkeypatch.setenv("XDG_CONFIG_HOME", invalida)
        assert logica._dir_de_config() == Path.home() / ".config"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert logica._dir_de_config() == Path.home() / ".config"


class _ApiDeJuguete(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Manejador)
        self.peticiones = []   # SimpleNamespace(metodo, ruta, autorizacion)
        self.respuestas = {}   # ruta sin query -> (código, cuerpo en bytes)


class _Manejador(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _responder(self):
        self.server.peticiones.append(SimpleNamespace(
            metodo=self.command, ruta=self.path,
            autorizacion=self.headers.get("Authorization")))
        codigo, cuerpo = self.server.respuestas.get(self.path.split("?")[0], (404, b"{}"))
        self.send_response(codigo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    do_GET = do_POST = _responder


@pytest.fixture
def api(logica, monkeypatch):
    """El bot y Lavalink en un solo servidor de juguete, con puerto efímero."""
    servidor = _ApiDeJuguete()
    threading.Thread(target=servidor.serve_forever, args=(0.01,), daemon=True).start()
    base = f"http://127.0.0.1:{servidor.server_address[1]}"
    monkeypatch.setattr(logica, "API_BASE", f"{base}/api")
    monkeypatch.setattr(logica, "LAVALINK_URL", f"{base}/version")
    monkeypatch.setattr(logica, "LAVALINK_PASSWORD", "clave")
    yield servidor
    servidor.shutdown()
    servidor.server_close()


@pytest.fixture
def bot_caido(logica, monkeypatch):
    """Nadie escucha. Doblado: en Windows un puerto cerrado tarda 2 s en rechazar."""
    def rechazada(*args, **kwargs):
        raise logica.urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(logica.urllib.request, "urlopen", rechazada)


def _json(datos):
    return 200, json.dumps(datos).encode()


def test_estado_bot_arriba_devuelve_lo_que_dice_la_api(logica, api):
    estado = {"usuario": "LavaDJ#1", "uptime": 12, "lavalink": {"conectado": True}}
    api.respuestas["/api/status"] = _json(estado)
    assert logica.estado_bot() == estado
    assert api.peticiones[0].metodo == "GET"


def test_estado_bot_abajo_es_none(logica, bot_caido):
    assert logica.estado_bot() is None


@pytest.mark.parametrize("respuesta", [(200, b"esto no es json"), (500, b"{}"), (404, b"{}")])
def test_estado_bot_con_respuesta_rota_es_none(logica, api, respuesta):
    api.respuestas["/api/status"] = respuesta
    assert logica.estado_bot() is None


def test_consultar_codifica_los_parametros_en_la_url_y_sin_ellos_es_un_get_simple(logica, api):
    api.respuestas["/api/headlines"] = _json({"titulares": [{"texto": "a"}]})
    api.respuestas["/api/dj_volume"] = _json({"ok": True})
    assert logica.consultar("headlines", timeout=2) == ("", {"titulares": [{"texto": "a"}]})
    assert logica.consultar("dj_volume", "POST", valor=50, motivo="el panel & yo") == ("", {"ok": True})
    assert [(p.metodo, p.ruta) for p in api.peticiones] == [
        ("GET", "/api/headlines"), ("POST", "/api/dj_volume?valor=50&motivo=el+panel+%26+yo")]


@pytest.mark.parametrize("cuerpo", [b"", b"no es json"])
def test_consultar_con_cuerpo_ilegible_no_es_error(logica, api, cuerpo):
    api.respuestas["/api/stop"] = (200, cuerpo)
    assert logica.consultar("stop") == ("", {})


def test_consultar_http_de_error_devuelve_el_motivo(logica, api):
    assert logica.consultar("no_existe") == ("Not Found", {})
    api.respuestas["/api/roto"] = (500, b"{}")
    assert logica.consultar("roto") == ("Internal Server Error", {})


def test_consultar_sin_bot_o_con_timeout_devuelve_un_error_legible(logica, bot_caido, monkeypatch):
    error, datos = logica.consultar("status")
    assert "refused" in error and datos == {}

    def agotado(*args, **kwargs):
        raise TimeoutError("se acabó el tiempo")
    monkeypatch.setattr(logica.urllib.request, "urlopen", agotado)
    assert logica.consultar("status") == ("se acabó el tiempo", {})


def test_postear_devuelve_vacio_si_salio_bien_y_pasa_el_motivo(logica, api):
    api.respuestas["/api/stop"] = _json({"ok": True})
    assert logica.postear("stop", motivo="boton Cortar del panel") == ""
    assert api.peticiones[0].metodo == "POST"
    assert api.peticiones[0].ruta == "/api/stop?motivo=boton+Cortar+del+panel"


def test_postear_sin_bot_devuelve_el_motivo_del_fallo(logica, bot_caido):
    assert logica.postear("stop")


def test_postear_con_http_de_error(logica, api):
    assert logica.postear("no_existe") == "Not Found"


@pytest.mark.parametrize("respuesta, esperado", [((200, b"4.0.8"), True), ((401, b""), False)])
def test_lavalink_manda_la_clave_y_si_la_rechaza_no_esta_arriba(logica, api, respuesta, esperado):
    api.respuestas["/version"] = respuesta
    assert logica.lavalink_arriba() is esperado
    assert api.peticiones[0].autorizacion == "clave"


def test_lavalink_abajo(logica, bot_caido):
    assert logica.lavalink_arriba() is False


def _doble_run(monkeypatch, logica, *, resultado=None, lanza=None):
    llamadas = []

    def run(comando, **kwargs):
        llamadas.append((comando, kwargs))
        if lanza:
            raise lanza
        return resultado
    monkeypatch.setattr(logica.subprocess, "run", run)
    return llamadas


def _completado(codigo, salida="", errores=""):
    return subprocess.CompletedProcess([], codigo, stdout=salida, stderr=errores)


def test_levantar_lavalink_ok(logica, monkeypatch):
    llamadas = _doble_run(monkeypatch, logica, resultado=_completado(0, "listo"))
    assert logica.levantar_lavalink() == ""
    comando, kwargs = llamadas[0]
    assert comando == [str(logica.LAVALINK_SCRIPT), "start"]
    assert kwargs["timeout"] == 180 and kwargs["capture_output"] and kwargs["text"]


def test_levantar_lavalink_fallido_devuelve_la_ultima_linea_corta_o_el_motivo(logica, monkeypatch):
    _doble_run(monkeypatch, logica, resultado=_completado(1, "uno\ndos\n", "tres\ncuatro\n"))
    assert logica.levantar_lavalink() == "cuatro"
    _doble_run(monkeypatch, logica, resultado=_completado(2, "", "x" * 300))
    assert logica.levantar_lavalink() == "x" * 120
    _doble_run(monkeypatch, logica, resultado=_completado(1, "  \n", ""))
    assert logica.levantar_lavalink() == "no pude levantar Lavalink"
    _doble_run(monkeypatch, logica, lanza=FileNotFoundError())
    assert logica.levantar_lavalink() == "falta start-lavalink.sh"
    _doble_run(monkeypatch, logica, lanza=subprocess.TimeoutExpired("cmd", 180))
    assert logica.levantar_lavalink() == "Lavalink tardó demasiado en levantar"


@pytest.fixture
def arranque(logica, monkeypatch, tmp_path):
    """Un proyecto de juguete con venv y `subprocess.Popen` doblado."""
    venv_python = tmp_path / "venv" / "python"
    venv_python.parent.mkdir()
    venv_python.write_bytes(b"")
    monkeypatch.setattr(logica, "PROJECT", tmp_path)
    monkeypatch.setattr(logica, "VENV_PYTHON", venv_python)
    monkeypatch.setattr(logica, "BOT_SCRIPT", tmp_path / "bot.py")
    monkeypatch.setattr(logica, "START_LOG", tmp_path / "logs" / "arranque.log")
    estado = SimpleNamespace(popen=[], falla=None)

    def popen(comando, **kwargs):
        estado.popen.append((comando, kwargs))
        if estado.falla:
            raise estado.falla
    monkeypatch.setattr(logica.subprocess, "Popen", popen)
    return estado


def test_levantar_bot_lanza_el_venv_con_el_log_de_arranque(logica, arranque, tmp_path):
    assert logica.levantar_bot() == ("", 0)
    comando, kwargs = arranque.popen[0]
    assert comando == [str(logica.VENV_PYTHON), str(tmp_path / "bot.py")]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stderr"] == subprocess.STDOUT and kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert Path(kwargs["stdout"].name) == logica.START_LOG
    assert kwargs["stdout"].closed   # el panel no se queda con el archivo abierto


def test_levantar_bot_devuelve_la_marca_del_log_previo(logica, arranque):
    _escribir(logica.START_LOG, "arranque anterior\n")
    assert logica.levantar_bot() == ("", len("arranque anterior\n"))


def test_levantar_bot_sin_venv_no_lanza_nada(logica, arranque):
    logica.VENV_PYTHON.unlink()
    error, marca = logica.levantar_bot()
    assert error == f"Falta el venv en {logica.VENV_PYTHON.parent}" and marca == 0
    assert arranque.popen == [] and not logica.START_LOG.parent.exists()


def test_levantar_bot_rota_el_log_grande_antes_de_tomar_la_marca(logica, arranque, monkeypatch):
    monkeypatch.setattr(logica, "ARRANQUE_MAXIMO", 10)
    _escribir(logica.START_LOG, "x" * 50)
    assert logica.levantar_bot() == ("", 0)
    assert logica.START_LOG.read_bytes() == b""
    assert (logica.START_LOG.parent / "arranque.log.1").read_bytes() == b"x" * 50


def test_rotar_arranque_deja_en_paz_un_log_pequeno_aguanta_fallos_y_sin_log_no_hace_nada(logica, arranque, monkeypatch):
    logica.rotar_arranque()
    assert not logica.START_LOG.exists()
    _escribir(logica.START_LOG, "x" * 50)
    logica.rotar_arranque()
    assert logica.START_LOG.read_bytes() == b"x" * 50
    monkeypatch.setattr(logica, "ARRANQUE_MAXIMO", 10)

    def no_se_puede(self, destino):
        raise PermissionError("bloqueado")
    monkeypatch.setattr(Path, "replace", no_se_puede)
    logica.rotar_arranque()   # no rota, pero tampoco revienta
    assert logica.START_LOG.read_bytes() == b"x" * 50


def test_levantar_bot_cuando_popen_falla(logica, arranque):
    _escribir(logica.START_LOG, "abc")
    arranque.falla = OSError(2, "No such file or directory")
    assert logica.levantar_bot() == ("No arrancó: No such file or directory", 3)
    assert arranque.popen[0][1]["stdout"].closed


def test_levantar_bot_si_no_puede_escribir_el_log(logica, arranque):
    logica.START_LOG.mkdir(parents=True)   # un directorio donde va el archivo
    error, marca = logica.levantar_bot()
    assert error.startswith("No se pudo escribir arranque.log: ")
    assert arranque.popen == []


TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "bot.py", line 3, in <module>\n'
    "    import wavelink\n"
    "ModuleNotFoundError: No module named 'wavelink'\n"
)


@pytest.fixture
def log_de_arranque(logica, monkeypatch, tmp_path):
    ruta = tmp_path / "arranque.log"
    monkeypatch.setattr(logica, "START_LOG", ruta)
    return ruta


VIEJO = "ValueError: de un arranque anterior\n"
WAVELINK = "ModuleNotFoundError: No module named 'wavelink'"


@pytest.mark.parametrize("contenido, marca, esperado", [
    ("arrancando...\n" + TRACEBACK, 0, WAVELINK),                       # la línea que cierra el traceback
    (VIEJO, 0, "ValueError: de un arranque anterior"),
    (VIEJO, len(VIEJO.encode()), ""),                                    # solo se lee lo escrito desde la marca
    (VIEJO + TRACEBACK, len(VIEJO.encode()), WAVELINK),
    ("\n\nboom\n\n  \n", 0, "boom"),                                   # ignora líneas en blanco
    ('Traceback (most recent call last):\n  File "x.py", line 1\n', 0, 'File "x.py", line 1'),   # todo parece traceback: la última
    ("E" * 500 + "\n", 0, "E" * 160),
    ("", 0, ""),
    (None, 0, ""),                                                       # sin archivo
])
def test_motivo_de_la_caida(logica, log_de_arranque, contenido, marca, esperado):
    if contenido is not None:
        _escribir(log_de_arranque, contenido)
    assert logica.motivo_de_la_caida(marca) == esperado


def test_motivo_aguanta_bytes_que_no_son_utf8(logica, log_de_arranque):
    log_de_arranque.write_bytes(b"\xff\xfe boom\n")
    assert logica.motivo_de_la_caida(0).endswith("boom")


LINEAS = [
    "2026-08-03 00:14:13,831 INFO bot: Bot listo",
    "2026-08-03 00:15:01,002 WARNING core.salud: flojo",
    "2026-08-03 00:16:44,500 ERROR cogs.music: se cayó",
]


def test_lineas_de_log_devuelve_las_ultimas_acortadas(logica, tmp_path):
    ruta = _escribir(tmp_path / "bot.log", "\n".join(LINEAS) + "\n")
    assert logica.lineas_de_log(fuente=ruta) == ["00:16  cogs.music: se cayó"]
    assert logica.lineas_de_log(2, ruta) == ["00:15  core.salud: flojo", "00:16  cogs.music: se cayó"]
    assert len(logica.lineas_de_log(50, ruta)) == 3


def test_lineas_de_log_salta_las_vacias_y_deja_los_tracebacks_y_sin_archivo_es_vacia(logica, tmp_path):
    ruta = _escribir(tmp_path / "bot.log", f"{LINEAS[0]}\n\n   \nTraceback (most recent call last):\n\n")
    assert logica.lineas_de_log(5, ruta) == ["00:14  bot: Bot listo", "Traceback (most recent call last):"]
    assert logica.lineas_de_log(3, tmp_path / "no_hay.log") == []


def test_lineas_de_log_lee_solo_la_cola_del_archivo(logica, tmp_path):
    total = 2000   # unos 80 KB, mucho más que lo que se lee
    ruta = _escribir(tmp_path / "bot.log", "".join(
        f"2026-08-03 10:00:00,000 INFO bot: linea {n}\n" for n in range(total)))
    ultimas = logica.lineas_de_log(5, ruta)
    assert ultimas == [f"10:00  bot: linea {n}" for n in range(total - 5, total)]
    assert len(logica.lineas_de_log(total, ruta)) < total


def test_lineas_de_log_aguanta_bytes_que_no_son_utf8(logica, tmp_path):
    ruta = tmp_path / "bot.log"
    ruta.write_bytes(b"\xff\xfe hola\n")
    assert logica.lineas_de_log(1, ruta)[0].endswith("hola")


@pytest.fixture
def ajustes(logica, monkeypatch, tmp_path):
    nuevo = tmp_path / "config" / "lavadj" / "panel.json"
    viejo = tmp_path / "config" / "huffybot" / "panel.json"
    monkeypatch.setattr(logica, "AJUSTES", nuevo)
    monkeypatch.setattr(logica, "AJUSTES_VIEJOS", viejo)
    return SimpleNamespace(nuevo=nuevo, viejo=viejo)


def test_leer_ajustes_sin_archivos_o_corrupto_es_vacio(logica, ajustes):
    assert logica.leer_ajustes() == {}
    _escribir(ajustes.nuevo, "{esto no es json")
    assert logica.leer_ajustes() == {}


def test_leer_ajustes_usa_el_viejo_solo_si_no_hay_nuevo_o_esta_corrupto(logica, ajustes):
    _escribir(ajustes.viejo, json.dumps({"luz": "noche"}))
    assert logica.leer_ajustes() == {"luz": "noche"}
    _escribir(ajustes.nuevo, "{roto")
    assert logica.leer_ajustes() == {"luz": "noche"}
    _escribir(ajustes.nuevo, json.dumps({"luz": "dia"}))
    assert logica.leer_ajustes() == {"luz": "dia"}


def test_guardar_ajuste_crea_las_carpetas_va_y_vuelve_y_conserva_las_otras_claves(logica, ajustes):
    logica.guardar_ajuste("luz", "noche")
    assert ajustes.nuevo.exists()
    logica.guardar_ajuste("dj_volumen", 0)
    logica.guardar_ajuste("luz", "dia")
    assert logica.leer_ajustes() == {"luz": "dia", "dj_volumen": 0}


def test_guardar_ajuste_migra_lo_del_archivo_viejo_sin_tocarlo(logica, ajustes):
    _escribir(ajustes.viejo, json.dumps({"luz": "noche"}))
    logica.guardar_ajuste("dj_volumen", 80)
    assert json.loads(ajustes.nuevo.read_text()) == {"luz": "noche", "dj_volumen": 80}
    assert json.loads(ajustes.viejo.read_text()) == {"luz": "noche"}


def test_guardar_ajuste_sobre_un_archivo_corrupto_lo_reemplaza(logica, ajustes):
    _escribir(ajustes.nuevo, "{roto")
    logica.guardar_ajuste("luz", "dia")
    assert logica.leer_ajustes() == {"luz": "dia"}


def test_guardar_ajuste_que_no_se_puede_escribir_no_revienta(logica, monkeypatch, tmp_path):
    bloqueo = _escribir(tmp_path / "bloqueo", "soy un archivo, no una carpeta")
    monkeypatch.setattr(logica, "AJUSTES", bloqueo / "lavadj" / "panel.json")
    monkeypatch.setattr(logica, "AJUSTES_VIEJOS", tmp_path / "no_hay.json")
    logica.guardar_ajuste("luz", "dia")
    assert logica.leer_ajustes() == {}


def test_el_modulo_solo_importa_la_biblioteca_estandar():
    """El panel corre con el python del sistema, que no tiene nada del venv."""
    arbol = ast.parse((SCRIPTS / "lavadj_panel_logica.py").read_text(encoding="utf-8"))
    importados = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.update(a.name.split(".")[0] for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            importados.add((nodo.module or "").split(".")[0])
    assert importados, "no encontró ningún import: ¿se rompió la prueba?"
    assert importados <= set(sys.stdlib_module_names), importados - set(sys.stdlib_module_names)


def _nombres_sin_definir(ruta):
    """Nombres que se leen y no se asignan, ni importan, ni definen, ni son builtins."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    definidos = set(dir(builtins)) | {"__file__", "__name__"}
    leidos = {}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name):
            if isinstance(nodo.ctx, ast.Load):
                leidos.setdefault(nodo.id, nodo.lineno)
            else:
                definidos.add(nodo.id)
        elif isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definidos.add(nodo.name)
        elif isinstance(nodo, ast.arg):
            definidos.add(nodo.arg)
        elif isinstance(nodo, (ast.Import, ast.ImportFrom)):
            definidos.update((a.asname or a.name).split(".")[0] for a in nodo.names)
        elif isinstance(nodo, ast.ExceptHandler) and nodo.name:
            definidos.add(nodo.name)
    return {n: linea for n, linea in leidos.items() if n not in definidos}


@pytest.mark.parametrize("archivo", ["lavadj-panel.py", "lavadj_panel_logica.py"])
def test_los_scripts_del_panel_no_usan_nombres_sin_definir(archivo):
    """Sin GTK no se puede ejecutar la ventana: esto atrapa lo que se movió y no se importó."""
    assert _nombres_sin_definir(SCRIPTS / archivo) == {}


def test_el_css_esta_junto_al_panel_y_cierra_sus_llaves():
    css = (SCRIPTS / "lavadj-panel.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}") > 50
    for selector in (".chasis", ".panel.noche", ".panel.dia", ".panel .testigo.al-aire"):
        assert selector in css
    assert "lavadj-panel.css" in (SCRIPTS / "lavadj-panel.py").read_text(encoding="utf-8")
