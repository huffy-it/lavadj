"""La lógica de `scripts/probar-que-suena.py` que se puede probar sin Discord.

El script conecta el bot real del dueño a un canal de voz y aquí no se ejecuta: se prueban sus decisiones
(canal, aviso de Lavalink de esta pista, qué es una falla, el resumen) con dobles. Lo único que toca la red
es un servidor de juguete en 127.0.0.1 que hace de API del bot. El nombre lleva guiones, de ahí
`spec_from_file_location`; que cargarlo no conecte a nada también se comprueba (todo va tras `__main__`).
"""

import asyncio
import importlib.util
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

RUTA = Path(__file__).resolve().parent.parent / "scripts" / "probar-que-suena.py"
YO = object()


@pytest.fixture(scope="module")
def pqs():
    spec = importlib.util.spec_from_file_location("probar_que_suena", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo    # antes de ejecutarlo: los dataclass buscan su módulo en sys.modules
    try:
        spec.loader.exec_module(modulo)
        yield modulo
    finally:
        sys.modules.pop(spec.name, None)


class Permisos:
    def __init__(self, connect=True, speak=True, move_members=False):
        self.connect, self.speak, self.move_members = connect, speak, move_members


class Miembro:
    def __init__(self, bot=False):
        self.bot = bot


class Canal:
    def __init__(self, nombre, permisos=None, miembros=(), user_limit=0):
        self.name, self.members, self.user_limit = nombre, list(miembros), user_limit
        self._permisos = permisos or Permisos()

    def permissions_for(self, yo):
        return self._permisos


class Pista:
    """La pista tal como sale de la búsqueda (o la que devuelve `play()`)."""

    def __init__(self, identifier="abc", uri="https://www.youtube.com/watch?v=abc",
                 encoded="enc-youtube", title="Un tema", author="Alguien", extras=None):
        self.identifier, self.uri, self.encoded = identifier, uri, encoded
        self.title, self.author, self.extras = title, author, extras or {}


def track_de_lavalink(encoded="enc-http", origen_id="abc", origen_uri="https://www.youtube.com/watch?v=abc"):
    """Lo que vuelve de Lavalink en un evento: cara de la fuente `http` + origen en userData."""
    extras = {"yt_origen": {"identifier": origen_id, "uri": origen_uri}} if origen_id else {}
    return Pista(identifier="rr4-loquesea", uri="https://rr4.googlevideo.com/videoplayback",
                 encoded=encoded, title="Unknown title", author="Unknown artist", extras=extras)


def ajena():
    return track_de_lavalink(encoded="enc-otra", origen_id="xyz", origen_uri="https://youtube/watch?v=xyz")


def aviso(pqs, tipo, track, **extra):
    return pqs.Evento(tipo, SimpleNamespace(track=track, original=None, **extra), time.monotonic())


class VcFalso:
    """Un player que, al tocar, deja en la cola los avisos que diga el guion."""

    def __init__(self, eventos, al_tocar=None, devuelve="sonando", error=None, cuelga=False):
        self.eventos, self.devuelve, self.error, self.cuelga = eventos, devuelve, error, cuelga
        self.al_tocar = al_tocar or (lambda pista, sonando: [])
        self.tocadas = []

    async def play(self, pista):
        self.tocadas.append(pista)
        if self.cuelga:
            await asyncio.sleep(3600)
        if self.error:
            raise self.error
        if self.devuelve is None:
            return None
        sonando = Pista(encoded="enc-http", identifier="abc", title="Un tema")
        for evento in self.al_tocar(pista, sonando):
            self.eventos.put_nowait(evento)
        return sonando


async def probar(pqs, guion=None, previos=(), timeout=1, segundos=0.05, **vc):
    """`probar_pista` con un `VcFalso` y la cola ya con los avisos `previos`."""
    eventos = asyncio.Queue()
    for evento in previos:
        eventos.put_nowait(evento)
    return await pqs.probar_pista(VcFalso(eventos, guion, **vc), eventos, Pista(), timeout=timeout, segundos=segundos)


def test_elegir_canal(pqs):
    con_gente, vacio = Canal("charla", miembros=[Miembro()]), Canal("vacío")
    assert pqs.elegir_canal([con_gente, vacio], YO) is vacio                       # prefiere el que no tiene humanos
    solo_bots = Canal("bots", miembros=[Miembro(bot=True), Miembro(bot=True)])
    assert pqs.elegir_canal([con_gente, solo_bots], YO) is solo_bots              # solo bots cuenta como vacío
    sin_conectar, mudo, bueno = Canal("a", permisos=Permisos(connect=False)), Canal("b", permisos=Permisos(speak=False)), Canal("c")
    assert pqs.elegir_canal([sin_conectar, mudo, bueno], YO) is bueno             # salta los que no dejan conectar o hablar
    assert pqs.elegir_canal([], YO) is None
    assert pqs.elegir_canal([sin_conectar], YO) is None


def test_elegir_canal_salta_el_que_esta_lleno_salvo_que_pueda_mover_miembros(pqs):
    lleno = Canal("lleno", miembros=[Miembro(bot=True), Miembro(bot=True)], user_limit=2)
    otro = Canal("otro")
    assert pqs.elegir_canal([lleno, otro], YO) is otro
    admin = Canal("lleno", miembros=[Miembro(bot=True), Miembro(bot=True)], user_limit=2, permisos=Permisos(move_members=True))
    assert pqs.elegir_canal([admin], YO) is admin


def test_elegir_canal_si_todos_tienen_gente_devuelve_el_primero_usable(pqs):
    sin_permiso = Canal("a", permisos=Permisos(connect=False), miembros=[Miembro()])
    primero, segundo = Canal("b", miembros=[Miembro()]), Canal("c", miembros=[Miembro(), Miembro()])
    assert pqs.elegir_canal([sin_permiso, primero, segundo], YO) is primero


def test_motivo_para_no_usar_dice_que_falta(pqs):
    assert "Conectar" in pqs.motivo_para_no_usar(Canal("a", permisos=Permisos(connect=False)), YO)
    assert "Hablar" in pqs.motivo_para_no_usar(Canal("a", permisos=Permisos(speak=False)), YO)
    assert pqs.motivo_para_no_usar(Canal("a"), YO) is None


def _guilds(*ids):
    return [SimpleNamespace(id=i) for i in ids]


def _servidor(id_, canales, nombre="Mi servidor"):
    return SimpleNamespace(id=id_, name=nombre, voice_channels=canales, me=YO)


def test_elegir_servidor(pqs):
    guilds = _guilds(1, 2, 3)
    assert pqs.elegir_servidor(guilds, frozenset()) is guilds[0]                  # sin lista blanca, el primero
    assert pqs.elegir_servidor(guilds, frozenset({2, 3})) is guilds[1]            # con ella, el primero permitido
    assert pqs.elegir_servidor(guilds, frozenset(), forzado=2) is guilds[1]
    assert pqs.elegir_servidor(guilds, frozenset(), forzado=9) is None            # forzado tiene que estar...
    assert pqs.elegir_servidor(guilds, frozenset({1}), forzado=2) is None         # ...y estar permitido
    assert pqs.elegir_servidor([], frozenset()) is None
    assert pqs.elegir_servidor(_guilds(1), frozenset({5})) is None


def test_elegir_destino_toma_el_primer_servidor_permitido_y_su_mejor_canal(pqs):
    vacio = Canal("vacío")
    cliente = SimpleNamespace(guilds=[
        _servidor(1, [Canal("x")]),
        _servidor(2, [Canal("charla", miembros=[Miembro()]), vacio]),
    ])
    guild, canal = pqs.elegir_destino(cliente, SimpleNamespace(canal=None, servidor=None), frozenset({2}))
    assert guild.id == 2 and canal is vacio


@pytest.mark.parametrize("guilds, canal, permitidos, aviso", [
    ([_servidor(1, [Canal("x")])], None, {99}, "SERVIDORES_PERMITIDOS"),
    ([_servidor(1, [Canal("a", permisos=Permisos(connect=False))])], None, set(), "--canal"),
    ([], 123, set(), "123"),
])
def test_elegir_destino_que_no_se_puede_explica_por_que(pqs, guilds, canal, permitidos, aviso):
    cliente = SimpleNamespace(guilds=guilds, get_channel=lambda id_: None)
    with pytest.raises(pqs.NoSePudoProbar, match=aviso):
        pqs.elegir_destino(cliente, SimpleNamespace(canal=canal, servidor=None), frozenset(permitidos))


async def test_el_cliente_anota_los_avisos_de_wavelink_con_su_hora(pqs):
    """El cableado que no se puede probar contra Discord: dispatch -> on_wavelink_* -> cola."""
    cliente = pqs.ClientePrueba()
    cliente.loop = asyncio.get_running_loop()    # `login()` lo fija; aquí no hay login
    payload = SimpleNamespace(track=Pista())
    for evento in ("track_start", "track_exception", "track_stuck", "track_end", "node_ready"):
        cliente.dispatch(f"wavelink_{evento}", payload)
    await asyncio.sleep(0.05)
    anotados = [cliente.eventos.get_nowait() for _ in range(4)]
    assert sorted(e.tipo for e in anotados) == ["arranco", "atascada", "excepcion", "termino"]
    assert all(e.payload is payload and e.t > 0 for e in anotados)
    assert cliente.eventos.empty() and cliente.nodo_listo.is_set()
    await cliente.close()


SONANDO = Pista(encoded="enc-http")


def _payload(track, original=None):
    return SimpleNamespace(track=track, original=original)


@pytest.mark.parametrize("payload, pista, sonando, esperado", [
    (_payload(track_de_lavalink(encoded="otro-encoded")), Pista(), Pista(encoded="enc-http"), True),                # origen en userData
    (_payload(track_de_lavalink(origen_id=None)), Pista(), Pista(encoded="enc-http"), True),                        # solo el encoded los une
    (_payload(Pista(identifier="abc", encoded="enc-plugin", uri="https://otra")), Pista(), None, True),             # sonó por el plugin
    (_payload(track_de_lavalink(encoded="x", origen_id=None), SONANDO), Pista(), SONANDO, True),   # el `original`
    (_payload(ajena()), Pista(), Pista(encoded="enc-http"), False),                                                 # aviso de otra pista
    (SimpleNamespace(track=None), Pista(), None, False),                                                            # sin pista
    (SimpleNamespace(), Pista(), None, False),
    (_payload(Pista(identifier="", uri="", encoded="", extras={"x": {"identifier": "", "uri": ""}})),
     Pista(identifier="", uri="", encoded=""), None, False),                                                        # "" == "" no es igual
])
def test_coincide(pqs, payload, pista, sonando, esperado):
    assert (pqs.coincide(payload, pista, sonando) if sonando else pqs.coincide(payload, pista)) is esperado


def test_motivo_de_fallo(pqs):
    motivo = pqs.motivo_de_fallo("excepcion", SimpleNamespace(
        exception={"message": "Something went wrong", "cause": "403 Forbidden", "severity": "common"}))
    assert "Something went wrong" in motivo and "403 Forbidden" in motivo
    assert pqs.motivo_de_fallo("excepcion", SimpleNamespace(exception=None))          # sin datos no revienta
    assert "10000" in pqs.motivo_de_fallo("atascada", SimpleNamespace(threshold=10000))
    assert pqs.motivo_de_fallo("termino", SimpleNamespace(reason="loadFailed"))        # el fin solo falla si no se pudo cargar
    assert pqs.motivo_de_fallo("termino", SimpleNamespace(reason="finished")) is None
    assert pqs.motivo_de_fallo("termino", SimpleNamespace(reason="replaced")) is None
    assert pqs.motivo_de_fallo("arranco", SimpleNamespace()) is None


def _arranca(pqs, *mas):
    return lambda pista, sonando: [aviso(pqs, "arranco", track_de_lavalink()), *mas]


async def test_una_pista_que_arranca_y_aguanta_es_ok(pqs):
    r = await probar(pqs, _arranca(pqs))
    assert r.ok is True and r.resolucion is not None
    assert r.segundos is not None and 0 <= r.segundos < 0.5


async def test_el_tiempo_de_arranque_sale_de_la_hora_del_aviso_no_de_la_de_lectura(pqs):
    def guion(pista, sonando):
        evento = aviso(pqs, "arranco", track_de_lavalink())
        evento.t = time.monotonic() + 0.1   # el aviso "llegó" 0.1 s después del play()
        return [evento]
    r = await probar(pqs, guion, timeout=2, segundos=0)
    assert 0.08 <= r.segundos < 0.4


@pytest.mark.parametrize("guion, motivo", [
    (lambda pqs: [aviso(pqs, "excepcion", track_de_lavalink(),
                        exception={"message": "No se pudo abrir la URL", "cause": "Read timed out"})],
     "No se pudo abrir la URL"),
    (lambda pqs: [aviso(pqs, "atascada", track_de_lavalink(), threshold=10000)], "atascó"),
], ids=["excepcion", "atascada"])
async def test_una_excepcion_o_un_atasco_de_lavalink_es_una_falla_con_su_motivo(pqs, guion, motivo):
    r = await probar(pqs, lambda pista, sonando: guion(pqs))
    assert r.ok is False and motivo in r.motivo


async def test_arranca_pero_falla_mientras_suena_es_una_falla(pqs):
    guion = _arranca(pqs, aviso(pqs, "excepcion", track_de_lavalink(),
                                exception={"message": "Connection reset", "cause": "googlevideo"}))
    r = await probar(pqs, guion, segundos=1)
    assert r.ok is False and "arrancó, pero" in r.motivo and "Connection reset" in r.motivo
    assert r.segundos is not None   # llegó a arrancar: el dato sirve igual


async def test_fallo_al_cargar_que_llega_como_fin_de_pista_es_una_falla(pqs):
    r = await probar(pqs, _arranca(pqs, aviso(pqs, "termino", track_de_lavalink(), reason="loadFailed")), segundos=1)
    assert r.ok is False


async def test_una_pista_corta_que_termina_sola_es_ok_sin_esperar_los_segundos_enteros(pqs):
    inicio = time.monotonic()
    r = await probar(pqs, _arranca(pqs, aviso(pqs, "termino", track_de_lavalink(), reason="finished")), segundos=30)
    assert r.ok is True and time.monotonic() - inicio < 2


async def test_el_aviso_ajeno_no_tapa_al_propio(pqs):
    previo = aviso(pqs, "excepcion", ajena(), exception={"message": "de la anterior"})
    r = await probar(pqs, _arranca(pqs), previos=[previo])
    assert r.ok is True


@pytest.mark.parametrize("opciones, motivo", [
    ({"timeout": 0.15}, "no arrancó en"),
    ({"timeout": 0.15, "ajeno": True}, "otras pistas"),     # la pista anterior, ya reemplazada, avisa tarde: no cuenta
    ({"error": RuntimeError("boom")}, "RuntimeError"),
    ({"cuelga": True, "timeout": 0.1}, "play()"),
    ({"devuelve": None}, "desconectó"),
], ids=["sin_avisos", "ajeno", "play_revienta", "play_cuelga", "desconectado"])
async def test_si_no_hay_aviso_propio_es_una_falla_con_su_motivo(pqs, opciones, motivo):
    opciones = dict(opciones)
    previos = [aviso(pqs, "arranco", ajena())] if opciones.pop("ajeno", False) else []
    r = await probar(pqs, previos=previos, **opciones)
    assert r.ok is False and motivo in r.motivo


def _args(**cosas):
    return SimpleNamespace(**{"consulta": None, "lista": None, "cuantas": 4, **cosas})


def _buscador(monkeypatch, pqs, respuestas):
    """Cambia `Playable.search` por una que contesta según la consulta (o lanza)."""
    consultadas = []

    async def buscar(consulta, **kwargs):
        consultadas.append(consulta)
        respuesta = respuestas[consulta]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta
    monkeypatch.setattr(pqs.wavelink.Playable, "search", staticmethod(buscar))
    return consultadas


async def test_reunir_pistas_sin_opciones_busca_las_de_por_defecto(pqs, monkeypatch):
    respuestas = {c: [Pista(identifier=f"id{i}"), Pista(identifier="otro")] for i, c in enumerate(pqs.CONSULTAS_POR_DEFECTO)}
    consultadas = _buscador(monkeypatch, pqs, respuestas)
    resultados = []
    pistas = await pqs.reunir_pistas(_args(), resultados)
    assert consultadas == list(pqs.CONSULTAS_POR_DEFECTO)
    assert [p.identifier for p in pistas] == ["id0", "id1", "id2", "id3"]    # el primer resultado de cada una, sin repetir videos
    assert resultados == []


async def test_reunir_pistas_consulta_reemplaza_las_de_por_defecto(pqs, monkeypatch):
    consultadas = _buscador(monkeypatch, pqs, {"mi tema": [Pista(identifier="m")]})
    pistas = await pqs.reunir_pistas(_args(consulta=["mi tema"]), [])
    assert consultadas == ["mi tema"] and [p.identifier for p in pistas] == ["m"]


async def test_reunir_pistas_lista_toma_las_primeras_n_de_la_playlist(pqs, monkeypatch):
    url = "https://www.youtube.com/watch?v=x&list=RDx"
    consultadas = _buscador(monkeypatch, pqs, {url: SimpleNamespace(tracks=[Pista(identifier=f"t{i}") for i in range(10)])})
    pistas = await pqs.reunir_pistas(_args(lista=url, cuantas=3), [])
    assert consultadas == [url]     # con --lista sola no se mezclan las búsquedas de por defecto
    assert [p.identifier for p in pistas] == ["t0", "t1", "t2"]


async def test_reunir_pistas_una_busqueda_rota_se_anota_como_falla(pqs, monkeypatch, capsys):
    _buscador(monkeypatch, pqs, {"rota": RuntimeError("Lavalink dijo que no"), "vacía": [], "buena": [Pista(identifier="b")]})
    resultados = []
    pistas = await pqs.reunir_pistas(_args(consulta=["rota", "vacía", "buena"]), resultados)
    assert [p.identifier for p in pistas] == ["b"]
    assert [r.ok for r in resultados] == [False, False]
    assert "Lavalink dijo que no" in resultados[0].motivo and "no devolvió resultados" in resultados[1].motivo
    assert capsys.readouterr().out.count("FALLA") == 2     # se ven en pantalla, no solo en el resumen


async def test_cerrar_sigue_adelante_aunque_falle_la_desconexion(pqs, monkeypatch):
    llamadas = []

    class Vc:
        async def disconnect(self):
            raise RuntimeError("ya estaba desconectado")

    class Cerrable:
        def __init__(self, nombre):
            self.nombre = nombre

        async def close(self):
            llamadas.append(self.nombre)

    async def cerrar_pool():
        llamadas.append("pool")
    monkeypatch.setattr(pqs.wavelink.Pool, "close", staticmethod(cerrar_pool))
    await pqs.cerrar(Cerrable("cliente"), Vc(), None, Cerrable("http"))
    assert llamadas == ["pool", "cliente", "http"]


def _ok(pqs, segundos, titulo="tema"):
    return pqs.Resultado(titulo, True, segundos=segundos, resolucion=segundos - 0.5)


def _falla(pqs, motivo="se rompió", titulo="tema"):
    return pqs.Resultado(titulo, False, motivo=motivo)


def test_linea_de_un_ok_lleva_titulo_y_segundos_y_la_de_una_falla_titulo_y_motivo(pqs):
    linea = pqs.linea_de(_ok(pqs, 2.94, "Never Gonna Give You Up"))
    assert linea.startswith("OK") and "Never Gonna Give You Up" in linea and "2.9 s" in linea
    linea = pqs.linea_de(_falla(pqs, "Lavalink no pudo reproducirla", "Bohemian Rhapsody"))
    assert linea.startswith("FALLA") and "Bohemian Rhapsody" in linea and "Lavalink no pudo reproducirla" in linea


def test_resumen_cuenta_las_que_sonaron_promedia_y_sin_ok_no_inventa_tiempos(pqs):
    texto = pqs.resumen([_ok(pqs, 2.0), _ok(pqs, 4.0), _falla(pqs), _ok(pqs, 3.0)])
    assert "3 de 4" in texto and "medio 3.0 s" in texto and "mínimo 2.0" in texto and "máximo 4.0" in texto
    texto = pqs.resumen([_falla(pqs), _falla(pqs)])
    assert "0 de 2" in texto and "medio" not in texto


def test_codigo_de_salida_y_primeras(pqs):
    assert pqs.codigo_de_salida([_ok(pqs, 1.0), _ok(pqs, 2.0)]) == 0
    assert pqs.codigo_de_salida([_ok(pqs, 1.0), _falla(pqs)]) == 1
    assert pqs.codigo_de_salida([]) == 1     # si no se probó nada, "todas sonaron" sería mentira
    assert pqs._primeras(["a", "b", "c"], 2) == ["a", "b"]
    assert pqs._primeras(SimpleNamespace(tracks=["a", "b", "c"]), 1) == ["a"]
    assert pqs._primeras([], 3) == []


def test_los_valores_por_defecto_y_consulta_se_puede_repetir(pqs):
    args = pqs.construir_parser().parse_args([])
    assert args.cortar_bot is False and args.consulta is None and args.lista is None
    assert (args.cuantas, args.timeout, args.segundos) == (4, 45, 5)
    assert pqs.construir_parser().parse_args(["--consulta", "uno", "--consulta", "dos"]).consulta == ["uno", "dos"]


def test_la_ayuda_esta_en_espanol(pqs):
    ayuda = pqs.construir_parser().format_help()
    assert ayuda.startswith("uso:") and "opciones:" in ayuda and "--cortar-bot" in ayuda and "Códigos de salida" in ayuda
    assert "show this help" not in ayuda and "options:" not in ayuda


@pytest.mark.parametrize("malos", [["--cuantas", "0"], ["--timeout", "0"], ["--segundos", "-1"]])
def test_validar_rechaza_valores_absurdos(pqs, malos):
    parser = pqs.construir_parser()
    with pytest.raises(SystemExit) as salida:
        pqs.validar(parser, parser.parse_args(malos))
    assert salida.value.code == 2


class _BotDeJuguete(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Manejador)
        self.recibidos = []

    @property
    def puerto(self):
        return self.server_address[1]


class _Manejador(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _responder(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        self._responder()

    def do_POST(self):
        self.server.recibidos.append((self.command, self.path, dict(self.headers)))
        self._responder()
        servidor = self.server    # como el bot de verdad: tras el POST, la API se apaga
        threading.Thread(target=lambda: (servidor.shutdown(), servidor.server_close()), daemon=True).start()


@pytest.fixture
def bot_de_juguete():
    servidor = _BotDeJuguete()
    threading.Thread(target=lambda: servidor.serve_forever(poll_interval=0.01), daemon=True).start()
    yield servidor
    try:
        servidor.shutdown()
    finally:
        servidor.server_close()


def _puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_bot_corriendo_dice_que_si_cuando_la_api_contesta_y_que_no_si_nadie_escucha(pqs, bot_de_juguete):
    assert pqs.bot_corriendo(bot_de_juguete.puerto) is True
    assert pqs.bot_corriendo(_puerto_libre(), timeout=0.2) is False    # corto: en Windows un puerto cerrado tarda ~2 s


def test_cortar_bot_manda_el_post_sin_origin_y_espera_a_que_calle(pqs, bot_de_juguete):
    assert pqs.cortar_bot(bot_de_juguete.puerto, espera=5, sondeo=0.05, timeout_sondeo=0.2) is True
    (metodo, ruta, cabeceras), = bot_de_juguete.recibidos
    assert metodo == "POST" and ruta.startswith("/api/stop")
    assert "origin" not in {nombre.lower() for nombre in cabeceras}     # la API rechaza los POST con un Origin ajeno
    assert cabeceras["Host"] == f"127.0.0.1:{bot_de_juguete.puerto}"
    assert pqs.bot_corriendo(bot_de_juguete.puerto, timeout=0.2) is False


def test_cortar_bot_dice_que_no_si_la_api_sigue_contestando(pqs, monkeypatch):
    def sin_servidor(*args, **kwargs):
        raise OSError("sin servidor")
    monkeypatch.setattr(pqs, "_abrir", sin_servidor)
    monkeypatch.setattr(pqs, "bot_corriendo", lambda puerto, timeout=2.0: True)
    assert pqs.cortar_bot(1, espera=0.2, sondeo=0.05) is False     # el POST falla y se ignora: cuenta que la API siga viva


def test_lavalink_que_no_contesta_da_un_motivo_con_la_receta(pqs):
    motivo = pqs.lavalink_no_contesta(f"http://127.0.0.1:{_puerto_libre()}", "clave", timeout=0.2)
    assert motivo and "start-lavalink.sh" in motivo
