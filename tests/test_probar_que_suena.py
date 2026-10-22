"""La lógica de `scripts/probar-que-suena.py` que se puede probar sin Discord.

El script conecta el bot real del dueño a un canal de voz, así que aquí no se
ejecuta nunca: se prueban las decisiones (qué canal elegir, si un aviso de Lavalink
es de esta pista, qué cuenta como falla, cómo se resume) con dobles simples. Lo
único que toca la red es un servidor de juguete en 127.0.0.1 que se hace pasar por
la API del bot.

El nombre del script lleva guiones y no se puede importar con `import`, de ahí
`spec_from_file_location`. Que cargarlo no conecte a nada es parte de lo que se
comprueba: todo lo que actúa va detrás de `if __name__ == "__main__"`.
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


@pytest.fixture(scope="module")
def pqs():
    spec = importlib.util.spec_from_file_location("probar_que_suena", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    # Registrado antes de ejecutarlo: los dataclass buscan su módulo en sys.modules.
    sys.modules[spec.name] = modulo
    try:
        spec.loader.exec_module(modulo)
        yield modulo
    finally:
        sys.modules.pop(spec.name, None)


# --- dobles ---------------------------------------------------------------------

class Permisos:
    def __init__(self, connect=True, speak=True, move_members=False):
        self.connect = connect
        self.speak = speak
        self.move_members = move_members


class Miembro:
    def __init__(self, bot=False):
        self.bot = bot


class Canal:
    def __init__(self, nombre, permisos=None, miembros=(), user_limit=0):
        self.name = nombre
        self.members = list(miembros)
        self.user_limit = user_limit
        self._permisos = permisos or Permisos()

    def permissions_for(self, yo):
        return self._permisos


YO = object()


class Pista:
    """La pista tal como sale de la búsqueda (o la que devuelve `play()`)."""

    def __init__(self, identifier="abc", uri="https://www.youtube.com/watch?v=abc",
                 encoded="enc-youtube", title="Un tema", author="Alguien", extras=None):
        self.identifier = identifier
        self.uri = uri
        self.encoded = encoded
        self.title = title
        self.author = author
        self.extras = extras or {}


def track_de_lavalink(encoded="enc-http", origen_id="abc", origen_uri="https://www.youtube.com/watch?v=abc"):
    """Lo que vuelve de Lavalink en un evento: cara de la fuente `http` + origen en userData."""
    extras = {"yt_origen": {"identifier": origen_id, "uri": origen_uri}} if origen_id else {}
    return Pista(identifier="rr4-loquesea", uri="https://rr4.googlevideo.com/videoplayback",
                 encoded=encoded, title="Unknown title", author="Unknown artist", extras=extras)


def aviso(pqs, tipo, track, **extra):
    payload = SimpleNamespace(track=track, original=None, **extra)
    return pqs.Evento(tipo, payload, time.monotonic())


class VcFalso:
    """Un player que, al tocar, deja en la cola los avisos que diga el guion."""

    def __init__(self, eventos, al_tocar=None, devuelve="sonando", error=None, cuelga=False):
        self.eventos = eventos
        self.al_tocar = al_tocar or (lambda pista, sonando: [])
        self.devuelve = devuelve
        self.error = error
        self.cuelga = cuelga
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


# --- elegir canal -----------------------------------------------------------------

def test_elegir_canal_prefiere_el_que_no_tiene_humanos(pqs):
    con_gente = Canal("charla", miembros=[Miembro()])
    vacio = Canal("vacío")
    assert pqs.elegir_canal([con_gente, vacio], YO) is vacio


def test_elegir_canal_un_canal_con_solo_bots_cuenta_como_vacio(pqs):
    solo_bots = Canal("bots", miembros=[Miembro(bot=True), Miembro(bot=True)])
    con_gente = Canal("charla", miembros=[Miembro()])
    assert pqs.elegir_canal([con_gente, solo_bots], YO) is solo_bots


def test_elegir_canal_salta_los_que_no_dejan_conectar_o_hablar(pqs):
    sin_conectar = Canal("a", permisos=Permisos(connect=False))
    mudo = Canal("b", permisos=Permisos(speak=False))
    bueno = Canal("c")
    assert pqs.elegir_canal([sin_conectar, mudo, bueno], YO) is bueno


def test_elegir_canal_salta_el_que_esta_lleno_salvo_que_pueda_mover_miembros(pqs):
    lleno = Canal("lleno", miembros=[Miembro(bot=True), Miembro(bot=True)], user_limit=2)
    otro = Canal("otro")
    assert pqs.elegir_canal([lleno, otro], YO) is otro

    lleno_pero_admin = Canal(
        "lleno", miembros=[Miembro(bot=True), Miembro(bot=True)], user_limit=2,
        permisos=Permisos(move_members=True),
    )
    assert pqs.elegir_canal([lleno_pero_admin], YO) is lleno_pero_admin


def test_elegir_canal_si_todos_tienen_gente_devuelve_el_primero_usable(pqs):
    sin_permiso = Canal("a", permisos=Permisos(connect=False), miembros=[Miembro()])
    primero = Canal("b", miembros=[Miembro()])
    segundo = Canal("c", miembros=[Miembro(), Miembro()])
    assert pqs.elegir_canal([sin_permiso, primero, segundo], YO) is primero


def test_elegir_canal_sin_ninguno_usable_devuelve_none(pqs):
    assert pqs.elegir_canal([], YO) is None
    assert pqs.elegir_canal([Canal("a", permisos=Permisos(connect=False))], YO) is None


def test_motivo_para_no_usar_dice_que_falta(pqs):
    assert "Conectar" in pqs.motivo_para_no_usar(Canal("a", permisos=Permisos(connect=False)), YO)
    assert "Hablar" in pqs.motivo_para_no_usar(Canal("a", permisos=Permisos(speak=False)), YO)
    assert pqs.motivo_para_no_usar(Canal("a"), YO) is None


# --- elegir servidor ----------------------------------------------------------------

def _guilds(*ids):
    return [SimpleNamespace(id=i) for i in ids]


def test_elegir_servidor_sin_lista_blanca_toma_el_primero(pqs):
    guilds = _guilds(1, 2)
    assert pqs.elegir_servidor(guilds, frozenset()) is guilds[0]


def test_elegir_servidor_con_lista_blanca_toma_el_primero_permitido(pqs):
    guilds = _guilds(1, 2, 3)
    assert pqs.elegir_servidor(guilds, frozenset({2, 3})) is guilds[1]


def test_elegir_servidor_forzado_tiene_que_estar_y_estar_permitido(pqs):
    guilds = _guilds(1, 2)
    assert pqs.elegir_servidor(guilds, frozenset(), forzado=2) is guilds[1]
    assert pqs.elegir_servidor(guilds, frozenset(), forzado=9) is None
    # Está el bot ahí, pero el dueño no lo quiere en ese servidor: no se prueba.
    assert pqs.elegir_servidor(guilds, frozenset({1}), forzado=2) is None


def test_elegir_servidor_sin_candidatos_devuelve_none(pqs):
    assert pqs.elegir_servidor([], frozenset()) is None
    assert pqs.elegir_servidor(_guilds(1), frozenset({5})) is None


def _servidor(id_, canales, nombre="Mi servidor"):
    return SimpleNamespace(id=id_, name=nombre, voice_channels=canales, me=YO)


def test_elegir_destino_toma_el_primer_servidor_permitido_y_su_mejor_canal(pqs):
    vacio = Canal("vacío")
    cliente = SimpleNamespace(guilds=[
        _servidor(1, [Canal("x")]),
        _servidor(2, [Canal("charla", miembros=[Miembro()]), vacio]),
    ])
    args = SimpleNamespace(canal=None, servidor=None)

    guild, canal = pqs.elegir_destino(cliente, args, frozenset({2}))

    assert guild.id == 2
    assert canal is vacio


def test_elegir_destino_sin_servidor_permitido_explica_la_lista_blanca(pqs):
    cliente = SimpleNamespace(guilds=[_servidor(1, [Canal("x")])])
    args = SimpleNamespace(canal=None, servidor=None)

    with pytest.raises(pqs.NoSePudoProbar, match="SERVIDORES_PERMITIDOS"):
        pqs.elegir_destino(cliente, args, frozenset({99}))


def test_elegir_destino_sin_canal_usable_pide_indicar_uno(pqs):
    sin_permiso = Canal("a", permisos=Permisos(connect=False))
    cliente = SimpleNamespace(guilds=[_servidor(1, [sin_permiso])])
    args = SimpleNamespace(canal=None, servidor=None)

    with pytest.raises(pqs.NoSePudoProbar, match="--canal"):
        pqs.elegir_destino(cliente, args, frozenset())


def test_elegir_destino_canal_que_no_es_de_voz_o_no_existe(pqs):
    cliente = SimpleNamespace(guilds=[], get_channel=lambda id_: None)
    args = SimpleNamespace(canal=123, servidor=None)

    with pytest.raises(pqs.NoSePudoProbar, match="123"):
        pqs.elegir_destino(cliente, args, frozenset())


async def test_el_cliente_anota_los_avisos_de_wavelink_con_su_hora(pqs):
    """El cableado que no se puede probar contra Discord: dispatch -> on_wavelink_* -> cola."""
    cliente = pqs.ClientePrueba()
    # `login()` fija el loop del cliente; aquí no hay login, así que se hace a mano.
    cliente.loop = asyncio.get_running_loop()
    payload = SimpleNamespace(track=Pista())

    cliente.dispatch("wavelink_track_start", payload)
    cliente.dispatch("wavelink_track_exception", payload)
    cliente.dispatch("wavelink_track_stuck", payload)
    cliente.dispatch("wavelink_track_end", payload)
    cliente.dispatch("wavelink_node_ready", payload)
    await asyncio.sleep(0.05)

    anotados = [cliente.eventos.get_nowait() for _ in range(4)]
    assert sorted(e.tipo for e in anotados) == ["arranco", "atascada", "excepcion", "termino"]
    assert all(e.payload is payload and e.t > 0 for e in anotados)
    assert cliente.eventos.empty()
    assert cliente.nodo_listo.is_set()
    await cliente.close()


# --- reconocer los avisos de Lavalink ---------------------------------------------

def test_coincide_por_el_origen_guardado_en_userdata(pqs):
    pista = Pista()
    evento = aviso(pqs, "arranco", track_de_lavalink(encoded="otro-encoded"))
    assert pqs.coincide(evento.payload, pista, Pista(encoded="enc-http")) is True


def test_coincide_por_el_encoded_de_lo_que_suena(pqs):
    pista = Pista(identifier="abc")
    sonando = Pista(encoded="enc-http")
    # Sin userData ni nada que se parezca: solo el encoded une el aviso con la pista.
    evento = aviso(pqs, "arranco", track_de_lavalink(encoded="enc-http", origen_id=None))
    assert pqs.coincide(evento.payload, pista, sonando) is True


def test_coincide_por_el_identifier_de_youtube_si_sono_por_el_plugin(pqs):
    pista = Pista(identifier="abc")
    track = Pista(identifier="abc", encoded="enc-plugin", uri="https://otra")
    assert pqs.coincide(SimpleNamespace(track=track, original=None), pista) is True


def test_coincide_acepta_el_original_que_wavelink_adjunta_al_arranque(pqs):
    sonando = Pista(encoded="enc-http")
    payload = SimpleNamespace(track=track_de_lavalink(encoded="x", origen_id=None), original=sonando)
    assert pqs.coincide(payload, Pista(identifier="abc"), sonando) is True


def test_no_coincide_con_el_aviso_de_otra_pista(pqs):
    pista = Pista(identifier="abc")
    otra = track_de_lavalink(encoded="enc-otra", origen_id="xyz", origen_uri="https://youtube/watch?v=xyz")
    assert pqs.coincide(SimpleNamespace(track=otra, original=None), pista, Pista(encoded="enc-http")) is False


def test_no_coincide_si_el_aviso_no_trae_pista(pqs):
    assert pqs.coincide(SimpleNamespace(track=None), Pista()) is False
    assert pqs.coincide(SimpleNamespace(), Pista()) is False


def test_coincide_no_se_deja_enganar_por_identificadores_vacios(pqs):
    # Dos pistas sin identifier ni uri no pueden darse por iguales por tener "" == "".
    pista = Pista(identifier="", uri="", encoded="")
    track = Pista(identifier="", uri="", encoded="", extras={"x": {"identifier": "", "uri": ""}})
    assert pqs.coincide(SimpleNamespace(track=track, original=None), pista) is False


# --- qué es un fallo ---------------------------------------------------------------

def test_motivo_de_fallo_de_una_excepcion_cuenta_mensaje_y_causa(pqs):
    payload = SimpleNamespace(exception={"message": "Something went wrong", "cause": "403 Forbidden",
                                         "severity": "common"})
    motivo = pqs.motivo_de_fallo("excepcion", payload)
    assert "Something went wrong" in motivo
    assert "403 Forbidden" in motivo


def test_motivo_de_fallo_de_una_excepcion_sin_datos_no_revienta(pqs):
    assert pqs.motivo_de_fallo("excepcion", SimpleNamespace(exception=None))


def test_motivo_de_fallo_de_una_pista_atascada(pqs):
    assert "10000" in pqs.motivo_de_fallo("atascada", SimpleNamespace(threshold=10000))


def test_motivo_de_fallo_el_fin_solo_falla_si_no_se_pudo_cargar(pqs):
    assert pqs.motivo_de_fallo("termino", SimpleNamespace(reason="loadFailed"))
    assert pqs.motivo_de_fallo("termino", SimpleNamespace(reason="finished")) is None
    assert pqs.motivo_de_fallo("termino", SimpleNamespace(reason="replaced")) is None


def test_motivo_de_fallo_el_arranque_no_es_un_fallo(pqs):
    assert pqs.motivo_de_fallo("arranco", SimpleNamespace()) is None


# --- probar una pista --------------------------------------------------------------

async def test_una_pista_que_arranca_y_aguanta_es_ok(pqs):
    eventos = asyncio.Queue()
    vc = VcFalso(eventos, lambda pista, sonando: [aviso(pqs, "arranco", track_de_lavalink())])

    r = await pqs.probar_pista(vc, eventos, Pista(), timeout=1, segundos=0.05)

    assert r.ok is True
    assert r.segundos is not None and 0 <= r.segundos < 0.5
    assert r.resolucion is not None


async def test_el_tiempo_de_arranque_sale_de_la_hora_del_aviso_no_de_la_de_lectura(pqs):
    eventos = asyncio.Queue()

    def guion(pista, sonando):
        evento = aviso(pqs, "arranco", track_de_lavalink())
        evento.t = time.monotonic() + 0.1   # el aviso "llegó" 0.1 s después del play()
        return [evento]

    r = await pqs.probar_pista(VcFalso(eventos, guion), eventos, Pista(), timeout=2, segundos=0)

    assert 0.08 <= r.segundos < 0.4


async def test_una_excepcion_de_lavalink_es_una_falla_con_su_motivo(pqs):
    eventos = asyncio.Queue()
    guion = lambda pista, sonando: [aviso(  # noqa: E731
        pqs, "excepcion", track_de_lavalink(),
        exception={"message": "No se pudo abrir la URL", "cause": "Read timed out"},
    )]

    r = await pqs.probar_pista(VcFalso(eventos, guion), eventos, Pista(), timeout=1, segundos=0.05)

    assert r.ok is False
    assert "No se pudo abrir la URL" in r.motivo


async def test_una_pista_atascada_es_una_falla(pqs):
    eventos = asyncio.Queue()
    guion = lambda pista, sonando: [aviso(pqs, "atascada", track_de_lavalink(), threshold=10000)]  # noqa: E731

    r = await pqs.probar_pista(VcFalso(eventos, guion), eventos, Pista(), timeout=1, segundos=0.05)

    assert r.ok is False
    assert "atascó" in r.motivo


async def test_sin_ningun_aviso_se_agota_el_timeout(pqs):
    eventos = asyncio.Queue()

    r = await pqs.probar_pista(VcFalso(eventos), eventos, Pista(), timeout=0.15, segundos=0.05)

    assert r.ok is False
    assert "no arrancó en" in r.motivo


async def test_los_avisos_de_otra_pista_se_descartan_y_se_nota_en_el_timeout(pqs):
    eventos = asyncio.Queue()
    ajena = track_de_lavalink(encoded="enc-otra", origen_id="xyz", origen_uri="https://youtube/watch?v=xyz")
    # La pista anterior, ya reemplazada, avisa tarde. No debe contar como la de ahora.
    eventos.put_nowait(aviso(pqs, "arranco", ajena))

    r = await pqs.probar_pista(VcFalso(eventos), eventos, Pista(), timeout=0.15, segundos=0.05)

    assert r.ok is False
    assert "otras pistas" in r.motivo


async def test_el_aviso_ajeno_no_tapa_al_propio(pqs):
    eventos = asyncio.Queue()
    ajena = track_de_lavalink(encoded="enc-otra", origen_id="xyz", origen_uri="https://youtube/watch?v=xyz")
    eventos.put_nowait(aviso(pqs, "excepcion", ajena, exception={"message": "de la anterior"}))
    vc = VcFalso(eventos, lambda pista, sonando: [aviso(pqs, "arranco", track_de_lavalink())])

    r = await pqs.probar_pista(vc, eventos, Pista(), timeout=1, segundos=0.05)

    assert r.ok is True


async def test_arranca_pero_falla_mientras_suena_es_una_falla(pqs):
    eventos = asyncio.Queue()
    guion = lambda pista, sonando: [  # noqa: E731
        aviso(pqs, "arranco", track_de_lavalink()),
        aviso(pqs, "excepcion", track_de_lavalink(),
              exception={"message": "Connection reset", "cause": "googlevideo"}),
    ]

    r = await pqs.probar_pista(VcFalso(eventos, guion), eventos, Pista(), timeout=1, segundos=1)

    assert r.ok is False
    assert "arrancó, pero" in r.motivo
    assert "Connection reset" in r.motivo
    assert r.segundos is not None   # llegó a arrancar: el dato sirve igual


async def test_una_pista_corta_que_termina_sola_es_ok_sin_esperar_los_segundos_enteros(pqs):
    eventos = asyncio.Queue()
    guion = lambda pista, sonando: [  # noqa: E731
        aviso(pqs, "arranco", track_de_lavalink()),
        aviso(pqs, "termino", track_de_lavalink(), reason="finished"),
    ]
    inicio = time.monotonic()

    r = await pqs.probar_pista(VcFalso(eventos, guion), eventos, Pista(), timeout=1, segundos=30)

    assert r.ok is True
    assert time.monotonic() - inicio < 2


async def test_fallo_al_cargar_que_llega_como_fin_de_pista_es_una_falla(pqs):
    eventos = asyncio.Queue()
    guion = lambda pista, sonando: [  # noqa: E731
        aviso(pqs, "arranco", track_de_lavalink()),
        aviso(pqs, "termino", track_de_lavalink(), reason="loadFailed"),
    ]

    r = await pqs.probar_pista(VcFalso(eventos, guion), eventos, Pista(), timeout=1, segundos=1)

    assert r.ok is False


async def test_si_play_revienta_es_una_falla_con_el_error(pqs):
    eventos = asyncio.Queue()

    r = await pqs.probar_pista(
        VcFalso(eventos, error=RuntimeError("boom")), eventos, Pista(), timeout=1, segundos=0.05
    )

    assert r.ok is False
    assert "RuntimeError" in r.motivo and "boom" in r.motivo


async def test_si_play_se_cuelga_se_corta_por_el_timeout(pqs):
    eventos = asyncio.Queue()

    r = await pqs.probar_pista(
        VcFalso(eventos, cuelga=True), eventos, Pista(), timeout=0.1, segundos=0.05
    )

    assert r.ok is False
    assert "play()" in r.motivo


async def test_si_el_player_se_desconecto_mientras_resolvia_es_una_falla(pqs):
    eventos = asyncio.Queue()

    r = await pqs.probar_pista(
        VcFalso(eventos, devuelve=None), eventos, Pista(), timeout=1, segundos=0.05
    )

    assert r.ok is False
    assert "desconectó" in r.motivo


# --- reunir las pistas y cerrar -------------------------------------------------------

def _args(**cosas):
    por_defecto = dict(consulta=None, lista=None, cuantas=4)
    por_defecto.update(cosas)
    return SimpleNamespace(**por_defecto)


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
    respuestas = {c: [Pista(identifier=f"id{i}"), Pista(identifier="otro")]
                  for i, c in enumerate(pqs.CONSULTAS_POR_DEFECTO)}
    consultadas = _buscador(monkeypatch, pqs, respuestas)
    resultados = []

    pistas = await pqs.reunir_pistas(_args(), resultados)

    assert consultadas == list(pqs.CONSULTAS_POR_DEFECTO)
    # El primer resultado de cada búsqueda, sin repetir videos.
    assert [p.identifier for p in pistas] == ["id0", "id1", "id2", "id3"]
    assert resultados == []


async def test_reunir_pistas_consulta_reemplaza_las_de_por_defecto(pqs, monkeypatch):
    consultadas = _buscador(monkeypatch, pqs, {"mi tema": [Pista(identifier="m")]})

    pistas = await pqs.reunir_pistas(_args(consulta=["mi tema"]), [])

    assert consultadas == ["mi tema"]
    assert [p.identifier for p in pistas] == ["m"]


async def test_reunir_pistas_lista_toma_las_primeras_n_de_la_playlist(pqs, monkeypatch):
    url = "https://www.youtube.com/watch?v=x&list=RDx"
    playlist = SimpleNamespace(tracks=[Pista(identifier=f"t{i}") for i in range(10)])
    consultadas = _buscador(monkeypatch, pqs, {url: playlist})

    pistas = await pqs.reunir_pistas(_args(lista=url, cuantas=3), [])

    # Con --lista sola no se mezclan las búsquedas de por defecto.
    assert consultadas == [url]
    assert [p.identifier for p in pistas] == ["t0", "t1", "t2"]


async def test_reunir_pistas_una_busqueda_rota_se_anota_como_falla(pqs, monkeypatch, capsys):
    _buscador(monkeypatch, pqs, {
        "rota": RuntimeError("Lavalink dijo que no"),
        "vacía": [],
        "buena": [Pista(identifier="b")],
    })
    resultados = []

    pistas = await pqs.reunir_pistas(_args(consulta=["rota", "vacía", "buena"]), resultados)

    assert [p.identifier for p in pistas] == ["b"]
    assert [r.ok for r in resultados] == [False, False]
    assert "Lavalink dijo que no" in resultados[0].motivo
    assert "no devolvió resultados" in resultados[1].motivo
    # Se ven en pantalla, no solo en el resumen.
    assert capsys.readouterr().out.count("FALLA") == 2


async def test_cerrar_sigue_adelante_aunque_falle_la_desconexion(pqs, monkeypatch):
    llamadas = []

    class Vc:
        async def disconnect(self):
            raise RuntimeError("ya estaba desconectado")

    class Cliente:
        async def close(self):
            llamadas.append("cliente")

    class SesionHttp:
        async def close(self):
            llamadas.append("http")

    async def cerrar_pool():
        llamadas.append("pool")

    monkeypatch.setattr(pqs.wavelink.Pool, "close", staticmethod(cerrar_pool))

    await pqs.cerrar(Cliente(), Vc(), None, SesionHttp())

    assert llamadas == ["pool", "cliente", "http"]


# --- el informe --------------------------------------------------------------------

def _ok(pqs, segundos, titulo="tema"):
    return pqs.Resultado(titulo, True, segundos=segundos, resolucion=segundos - 0.5)


def _falla(pqs, motivo="se rompió", titulo="tema"):
    return pqs.Resultado(titulo, False, motivo=motivo)


def test_linea_de_un_ok_lleva_titulo_y_segundos(pqs):
    linea = pqs.linea_de(_ok(pqs, 2.94, "Never Gonna Give You Up"))
    assert linea.startswith("OK")
    assert "Never Gonna Give You Up" in linea
    assert "2.9 s" in linea


def test_linea_de_una_falla_lleva_titulo_y_motivo(pqs):
    linea = pqs.linea_de(_falla(pqs, "Lavalink no pudo reproducirla", "Bohemian Rhapsody"))
    assert linea.startswith("FALLA")
    assert "Bohemian Rhapsody" in linea
    assert "Lavalink no pudo reproducirla" in linea


def test_resumen_cuenta_las_que_sonaron_y_promedia_el_arranque(pqs):
    texto = pqs.resumen([_ok(pqs, 2.0), _ok(pqs, 4.0), _falla(pqs), _ok(pqs, 3.0)])
    assert "3 de 4" in texto
    assert "medio 3.0 s" in texto
    assert "mínimo 2.0" in texto and "máximo 4.0" in texto


def test_resumen_sin_ninguna_ok_no_inventa_tiempos(pqs):
    texto = pqs.resumen([_falla(pqs), _falla(pqs)])
    assert "0 de 2" in texto
    assert "medio" not in texto


def test_codigo_de_salida(pqs):
    assert pqs.codigo_de_salida([_ok(pqs, 1.0), _ok(pqs, 2.0)]) == 0
    assert pqs.codigo_de_salida([_ok(pqs, 1.0), _falla(pqs)]) == 1
    # Si no se probó nada, "todas sonaron" sería mentira.
    assert pqs.codigo_de_salida([]) == 1


def test_primeras_toma_de_una_lista_o_de_una_playlist(pqs):
    assert pqs._primeras(["a", "b", "c"], 2) == ["a", "b"]
    assert pqs._primeras(SimpleNamespace(tracks=["a", "b", "c"]), 1) == ["a"]
    assert pqs._primeras([], 3) == []


# --- la línea de comandos -------------------------------------------------------------

def test_los_valores_por_defecto(pqs):
    args = pqs.construir_parser().parse_args([])
    assert args.cortar_bot is False
    assert args.consulta is None and args.lista is None
    assert args.cuantas == 4
    assert args.timeout == 45
    assert args.segundos == 5


def test_consulta_se_puede_repetir(pqs):
    args = pqs.construir_parser().parse_args(["--consulta", "uno", "--consulta", "dos"])
    assert args.consulta == ["uno", "dos"]


def test_la_ayuda_esta_en_espanol(pqs):
    ayuda = pqs.construir_parser().format_help()
    assert ayuda.startswith("uso:")
    assert "opciones:" in ayuda
    assert "--cortar-bot" in ayuda
    assert "Códigos de salida" in ayuda
    assert "show this help" not in ayuda and "options:" not in ayuda


def test_validar_rechaza_valores_absurdos(pqs):
    parser = pqs.construir_parser()
    for malos in (["--cuantas", "0"], ["--timeout", "0"], ["--segundos", "-1"]):
        with pytest.raises(SystemExit) as salida:
            pqs.validar(parser, parser.parse_args(malos))
        assert salida.value.code == 2


# --- hablar con la API del bot -----------------------------------------------------------

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
        cuerpo = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        self._responder()

    def do_POST(self):
        self.server.recibidos.append((self.command, self.path, dict(self.headers)))
        self._responder()
        # Como el bot de verdad: tras el POST, la API se apaga.
        servidor = self.server
        threading.Thread(
            target=lambda: (servidor.shutdown(), servidor.server_close()), daemon=True
        ).start()


@pytest.fixture
def bot_de_juguete():
    servidor = _BotDeJuguete()
    hilo = threading.Thread(
        target=lambda: servidor.serve_forever(poll_interval=0.01), daemon=True
    )
    hilo.start()
    yield servidor
    try:
        servidor.shutdown()
    finally:
        servidor.server_close()


def _puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_bot_corriendo_dice_que_si_cuando_la_api_contesta(pqs, bot_de_juguete):
    assert pqs.bot_corriendo(bot_de_juguete.puerto) is True


def test_bot_corriendo_dice_que_no_si_nadie_escucha(pqs):
    # Timeout corto: en Windows un puerto cerrado tarda ~2 s en rechazar la conexión.
    assert pqs.bot_corriendo(_puerto_libre(), timeout=0.2) is False


def test_cortar_bot_manda_el_post_sin_origin_y_espera_a_que_calle(pqs, bot_de_juguete):
    assert pqs.cortar_bot(bot_de_juguete.puerto, espera=5, sondeo=0.05, timeout_sondeo=0.2) is True

    (metodo, ruta, cabeceras), = bot_de_juguete.recibidos
    assert metodo == "POST"
    assert ruta.startswith("/api/stop")
    # La API rechaza los POST con un Origin ajeno: el nuestro no debe llevar ninguno.
    assert "origin" not in {nombre.lower() for nombre in cabeceras}
    assert cabeceras["Host"] == f"127.0.0.1:{bot_de_juguete.puerto}"
    assert pqs.bot_corriendo(bot_de_juguete.puerto, timeout=0.2) is False


def test_cortar_bot_dice_que_no_si_la_api_sigue_contestando(pqs, monkeypatch):
    def sin_servidor(*args, **kwargs):
        raise OSError("sin servidor")

    monkeypatch.setattr(pqs, "_abrir", sin_servidor)
    monkeypatch.setattr(pqs, "bot_corriendo", lambda puerto, timeout=2.0: True)
    # El POST falla y se ignora: lo que cuenta es que la API siga viva.
    assert pqs.cortar_bot(1, espera=0.2, sondeo=0.05) is False


def test_lavalink_que_no_contesta_da_un_motivo_con_la_receta(pqs):
    motivo = pqs.lavalink_no_contesta(f"http://127.0.0.1:{_puerto_libre()}", "clave", timeout=0.2)
    assert motivo and "start-lavalink.sh" in motivo
