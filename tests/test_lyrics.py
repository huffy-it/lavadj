"""Las letras: parseo de LRC, preferencia por la sincronizada, caché y `find`. Sin red: la sesión de aiohttp es un doble."""

import asyncio

import aiohttp
import pytest

from core import lyrics
from core.lyrics import Letra


@pytest.mark.parametrize("texto, esperado", [
    ("[00:12.34]hola\n[01:02.5]sigo\n[02:00]fin\n[00:03.456]casi",
     [(3456, "casi"), (12340, "hola"), (62500, "sigo"), (120000, "fin")]),
    ("[100:00.00]larguísima", [(6_000_000, "larguísima")]),
    ("[00:12.34][01:02.00]estribillo\n[00:30.00]verso", [(12340, "estribillo"), (30000, "verso"), (62000, "estribillo")]),
    ("[ar:Alguien]\n[ti:Algo]\n[al:Disco]\n[by:yo]\n[length: 03:20]\n[00:01.00]letra", [(1000, "letra")]),
    ("[offset:+500]\n[00:12.34]hola", [(11840, "hola")]),
    ("[offset:-500]\n[00:12.34]hola", [(12840, "hola")]),
    ("[offset: 500 ]\n[00:12.34]hola", [(11840, "hola")]),
    ("[offset:0]\n[00:12.34]hola", [(12340, "hola")]),
    ("[offset:+5000]\n[00:01.00]hola", [(0, "hola")]),
    ("[00:10.00]hola\n[offset:+1000]", [(9000, "hola")]),
    ("[00:20.00]c\n[00:05.00]a\n[00:20.00]d\n[00:10.00]b", [(5000, "a"), (10000, "b"), (20000, "c"), (20000, "d")]),
    ("\n\ncréditos sin marca\n[00:01.00]hola\n   \n[00:02.00]  chau  \n", [(1000, "hola"), (2000, "chau")]),
    ("[00:01.00]uno\n[00:05.00]\n[00:06.00]\n[00:09.00]dos", [(1000, "uno"), (5000, ""), (9000, "dos")]),
    ("[00:00.00]\n[00:01.00]uno\n[00:05.00]\n[00:09.00]", [(1000, "uno")]),
    ("[00:01.00]uno\r\n[00:02.00]dos\r\n", [(1000, "uno"), (2000, "dos")]),
], ids=["minutos_segundos_fraccion", "minutos_de_3_cifras", "varias_marcas", "metadatos", "offset_positivo", "offset_negativo",
        "offset_con_espacios", "offset_cero", "offset_sin_negativos", "offset_tras_las_lineas", "orden_y_empates",
        "sin_marca_ni_vacias", "pausas_del_medio", "pausas_de_los_bordes", "saltos_de_windows"])
def test_parse_lrc(texto, esperado):
    assert lyrics.parse_lrc(texto) == esperado


@pytest.mark.parametrize("texto", ["", None, "solo texto\nsin marcas", "[ar:X]\n[ti:Y]", "[00:01.00]\n[00:02.00]"])
def test_parse_lrc_sin_lineas_con_tiempo_da_lista_vacia(texto):
    assert lyrics.parse_lrc(texto) == []


def test_plain_letra_separa_en_lineas_y_marca_que_no_esta_sincronizada():
    assert lyrics.plain_letra("uno\ndos\n\ntres") == Letra(False, [(None, "uno"), (None, "dos"), (None, ""), (None, "tres")])


@pytest.mark.parametrize("texto", ["", "   \n\n ", None])
def test_plain_letra_vacia_es_none(texto):
    assert lyrics.plain_letra(texto) is None


UNO_DOS = Letra(False, [(None, "uno"), (None, "dos")])


@pytest.mark.parametrize("resultados, duracion_ms, esperada", [
    ([{"plainLyrics": "solo texto"}, {"plainLyrics": "texto", "syncedLyrics": "[00:01.00]con tiempos"}], 0,
     Letra(True, [(1000, "con tiempos")])),
    ([{"plainLyrics": ""}, {"syncedLyrics": "no es lrc"}, {"plainLyrics": "uno\ndos"}], 0, UNO_DOS),
    ([{"duration": 300, "syncedLyrics": "[00:01.00]directo"}, {"duration": 212, "syncedLyrics": "[00:01.00]estudio"},
      {"syncedLyrics": "[00:01.00]sin duración"}], 213_000, Letra(True, [(1000, "estudio")])),
    ([{"duration": 300, "syncedLyrics": "[00:01.00]primera"}, {"duration": 212, "syncedLyrics": "[00:01.00]otra"}], 0,
     Letra(True, [(1000, "primera")])),
    *[(r, 0, None) for r in ([], [{}], [{"plainLyrics": None, "syncedLyrics": None}], [{"instrumental": True}])],
], ids=["prefiere_sincronizada", "cae_a_plano", "duracion_mas_cercana", "sin_duracion_la_primera",
        "vacia", "sin_campos", "campos_none", "instrumental"])
def test_pick_letra(resultados, duracion_ms, esperada):
    assert lyrics.pick_letra(resultados, duracion_ms) == esperada


@pytest.mark.parametrize("titulo, autor, esperado", [
    ("Soda Stereo - De Música Ligera (Official Video)", "SodaStereoVEVO", ("Soda Stereo", "De Música Ligera")),
    ("Never Gonna Give You Up", "Rick Astley - Topic", ("Rick Astley", "Never Gonna Give You Up")),
    ("Never Gonna Give You Up", "Rick Astley", ("Rick Astley", "Never Gonna Give You Up")),
    ("Never Gonna Give You Up", "RickAstleyVEVO", ("RickAstley", "Never Gonna Give You Up")),
    ("Never Gonna Give You Up", "", ("", "Never Gonna Give You Up")),
])
def test_artista_y_cancion_salen_del_titulo_o_si_no_del_canal(titulo, autor, esperado):
    assert lyrics.artist_and_song(titulo, autor) == esperado


class Respuesta:
    def __init__(self, datos, status=200):
        self.datos, self.status = datos, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.datos


LRCLIB, OVH = "https://lrclib.net/api/search", "https://api.lyrics.ovh/v1/"


class SesionFalsa:
    """`lrclib(params)` y `ovh(artista, cancion)` devuelven lo que se contesta (o lanzan el fallo). Sin `ovh`, contesta 404."""

    def __init__(self, lrclib=None, ovh=None):
        self.lrclib, self.ovh, self.pedidos = lrclib or (lambda params: []), ovh, []

    def get(self, url, params=None, timeout=None):
        self.pedidos.append((url, params))
        assert timeout is not None, "toda petición lleva tiempo límite"
        if url == LRCLIB:
            return Respuesta(self.lrclib(params))
        assert url.startswith(OVH), url
        return Respuesta(self.ovh(*url.removeprefix(OVH).split("/"))) if self.ovh else Respuesta({}, 404)

    @property
    def busquedas(self):
        return [params for url, params in self.pedidos if url == LRCLIB]

    @property
    def a_ovh(self):
        return [url for url, _ in self.pedidos if url.startswith(OVH)]


CON_TIEMPOS = {"plainLyrics": "uno\ndos", "syncedLyrics": "[00:01.00]uno\n[00:02.00]dos"}


@pytest.mark.parametrize("respuesta, esperada", [
    ([CON_TIEMPOS], Letra(True, [(1000, "uno"), (2000, "dos")])),
    ([{"plainLyrics": "uno\ndos"}], UNO_DOS),
    ({"error": "mal"}, None),
], ids=["con_tiempos", "texto_plano", "no_es_una_lista"])
async def test_find_synced_devuelve_la_letra_parseada_o_cae_a_texto_plano(respuesta, esperada):
    assert await lyrics.find_synced(SesionFalsa(lrclib=lambda params: respuesta), "A - T", "A", "T") == esperada


async def test_find_synced_usa_las_mismas_busquedas_que_find():
    titulo, artista, cancion = "Soda Stereo - De Música Ligera (Official Video)", "Soda Stereo", "De Música Ligera"
    synced, plano = SesionFalsa(), SesionFalsa()
    await lyrics.find_synced(synced, titulo, artista, cancion)
    await lyrics.find(plano, titulo, artista, cancion)

    assert synced.busquedas == plano.busquedas
    assert synced.busquedas[:2] == [{"track_name": cancion, "artist_name": artista}, {"track_name": cancion}]
    assert {"q": "Soda Stereo De Música Ligera"} in synced.busquedas


@pytest.mark.parametrize("vacias, busquedas", [(0, 1), (1, 2)])
async def test_find_synced_se_queda_con_la_primera_busqueda_que_da_algo(vacias, busquedas):
    sesion = SesionFalsa(lrclib=lambda params: [] if len(sesion.busquedas) <= vacias else [CON_TIEMPOS])

    letra = await lyrics.find_synced(sesion, "A - T", "A", "T")

    assert letra.sincronizada and len(sesion.busquedas) == busquedas


async def test_find_synced_elige_por_duracion():
    sesion = SesionFalsa(lrclib=lambda params: [
        {"duration": 300, "syncedLyrics": "[00:01.00]directo"}, {"duration": 200, "syncedLyrics": "[00:01.00]estudio"},
    ])
    letra = await lyrics.find_synced(sesion, "A - T", "A", "T", duracion_ms=201_000)
    assert letra.lineas == [(1000, "estudio")]


async def test_find_synced_cae_a_lyrics_ovh_sin_tiempos():
    sesion = SesionFalsa(ovh=lambda artista, cancion: {"lyrics": "de ovh\nlínea dos"})

    letra = await lyrics.find_synced(sesion, "Artista - Tema", "Artista", "Tema")

    assert letra == Letra(False, [(None, "de ovh"), (None, "línea dos")])
    assert sesion.a_ovh == ["https://api.lyrics.ovh/v1/Artista/Tema"]


async def test_find_synced_sin_artista_no_pregunta_a_lyrics_ovh():
    sesion = SesionFalsa(ovh=lambda *_: pytest.fail("no debía llamar"))
    assert await lyrics.find_synced(sesion, "Tema", "", "Tema") is None
    assert sesion.a_ovh == []


@pytest.mark.parametrize("fallo", [aiohttp.ClientError("caído"), asyncio.TimeoutError()])
async def test_find_synced_si_la_red_falla_da_none_sin_romper(fallo):
    def revienta(*_):
        raise fallo

    assert await lyrics.find_synced(SesionFalsa(lrclib=revienta, ovh=revienta), "A - T", "A", "T") is None


@pytest.mark.parametrize("resultados, esperado", [
    ([{"plainLyrics": "texto plano", "syncedLyrics": "[00:01.00]otro"}], "texto plano"),
    ([{"syncedLyrics": "[00:01.00]uno\n[00:02.50]dos"}], "uno\ndos"),
    ([{"plainLyrics": ""}, {}, {"plainLyrics": "la buena"}], "la buena"),
    ([], None),
], ids=["prefiere_plain", "synced_sin_marcas", "salta_sin_letra", "nada_en_ningun_lado"])
async def test_find_devuelve_el_texto_plano(resultados, esperado):
    assert await lyrics.find(SesionFalsa(lrclib=lambda params: resultados), "A - T", "A", "T") == esperado


async def test_find_cae_a_lyrics_ovh_y_sin_artista_no_pregunta():
    con = SesionFalsa(ovh=lambda artista, cancion: {"lyrics": "de ovh"})
    assert await lyrics.find(con, "Artista - Tema", "Artista", "Tema") == "de ovh"

    sin = SesionFalsa(ovh=lambda *_: pytest.fail("no debía llamar"))
    assert await lyrics.find(sin, "Tema", "", "Tema") is None


class Buscador:
    """`buscar` de juguete: cuenta las veces que se lo llama y contesta lo que se le diga."""

    def __init__(self, letra=Letra(True, [(0, "hola")]), demora=0.0):
        self.letra, self.demora, self.llamadas = letra, demora, 0

    async def __call__(self):
        self.llamadas += 1
        await asyncio.sleep(self.demora)
        return self.letra


class Reloj:
    t = 0.0

    def __call__(self):
        return self.t


HOLA = Letra(True, [(0, "hola")])


async def test_la_cache_no_repite_la_busqueda_de_una_cancion_y_distingue_las_canciones():
    letras, buscar = lyrics.Letras(), Buscador()

    primera, segunda = await letras.obtener("abc", buscar), await letras.obtener("abc", buscar)
    assert primera == segunda == HOLA and buscar.llamadas == 1

    await letras.obtener("xyz", buscar)
    assert buscar.llamadas == 2


async def test_el_no_encontrado_se_recuerda_pero_caduca_y_la_letra_no():
    reloj = Reloj()
    letras = lyrics.Letras(caduca_vacia=60, reloj=reloj)
    sin, con = Buscador(letra=None), Buscador()
    await letras.obtener("sin", sin)
    await letras.obtener("con", con)

    reloj.t = 59
    assert await letras.obtener("sin", sin) is None
    assert sin.llamadas == 1

    reloj.t = 61
    await letras.obtener("sin", sin)
    await letras.obtener("con", con)
    assert (sin.llamadas, con.llamadas) == (2, 1)


async def test_la_cache_esta_acotada_y_saca_la_menos_usada():
    letras, buscar = lyrics.Letras(maximo=2), Buscador()
    for clave in ("a", "b", "a", "c"):      # `a` vuelve a ser la más reciente; entra `c`, sale `b`
        await letras.obtener(clave, buscar)
    assert buscar.llamadas == 3

    await letras.obtener("a", buscar)
    await letras.obtener("c", buscar)
    assert buscar.llamadas == 3

    await letras.obtener("b", buscar)
    assert buscar.llamadas == 4 and len(letras._datos) == 2


async def test_las_busquedas_simultaneas_de_la_misma_cancion_comparten_una():
    letras, buscar = lyrics.Letras(), Buscador(demora=0.01)
    resultados = await asyncio.gather(*(letras.obtener("abc", buscar) for _ in range(4)))
    assert buscar.llamadas == 1
    assert all(r == HOLA for r in resultados)


async def test_si_quien_pregunta_se_cancela_la_busqueda_termina_y_queda_en_cache():
    letras, buscar = lyrics.Letras(), Buscador(demora=0.02)

    tarea = asyncio.ensure_future(letras.obtener("abc", buscar))
    await asyncio.sleep(0)
    tarea.cancel()
    await asyncio.sleep(0.05)

    assert await letras.obtener("abc", buscar) == HOLA
    assert buscar.llamadas == 1


async def test_una_busqueda_que_se_pasa_del_tiempo_cuenta_como_no_encontrada(monkeypatch):
    monkeypatch.setattr(lyrics, "TOTAL_TIMEOUT", 0.01)
    letras, buscar = lyrics.Letras(), Buscador(demora=1)
    assert await letras.obtener("abc", buscar) is None
    assert await letras.obtener("abc", buscar) is None
    assert buscar.llamadas == 1


async def test_un_error_inesperado_sale_y_no_se_guarda():
    letras, llamadas = lyrics.Letras(), []

    async def buscar():
        llamadas.append(1)
        raise RuntimeError("bug")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await letras.obtener("abc", buscar)
    assert len(llamadas) == 2 and not letras._en_vuelo
