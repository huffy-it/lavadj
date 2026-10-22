"""Las letras: el parseo de LRC, la preferencia por la sincronizada, la caché y que `find` siga igual.

Sin red: la sesión de aiohttp es un doble que responde según la búsqueda.
"""

import asyncio

import aiohttp
import pytest

from core import lyrics
from core.lyrics import Letra


# --- parseo de LRC ----------------------------------------------------------

def test_parse_lrc_lee_minutos_segundos_y_fraccion():
    assert lyrics.parse_lrc("[00:12.34]hola\n[01:02.5]sigo\n[02:00]fin\n[00:03.456]casi") == [
        (3456, "casi"), (12340, "hola"), (62500, "sigo"), (120000, "fin"),
    ]


def test_parse_lrc_acepta_minutos_de_mas_de_dos_cifras():
    assert lyrics.parse_lrc("[100:00.00]larguísima") == [(6_000_000, "larguísima")]


def test_parse_lrc_una_linea_con_varias_marcas_cuenta_una_vez_por_marca():
    assert lyrics.parse_lrc("[00:12.34][01:02.00]estribillo\n[00:30.00]verso") == [
        (12340, "estribillo"), (30000, "verso"), (62000, "estribillo"),
    ]


def test_parse_lrc_descarta_las_etiquetas_de_metadatos():
    texto = "[ar:Alguien]\n[ti:Algo]\n[al:Disco]\n[by:yo]\n[length: 03:20]\n[00:01.00]letra"
    assert lyrics.parse_lrc(texto) == [(1000, "letra")]


@pytest.mark.parametrize("etiqueta, esperado", [
    ("[offset:+500]", 11840), ("[offset:-500]", 12840), ("[offset: 500 ]", 11840), ("[offset:0]", 12340),
])
def test_parse_lrc_aplica_el_offset(etiqueta, esperado):
    """Un offset positivo adelanta la letra: cada línea sale antes."""
    assert lyrics.parse_lrc(f"{etiqueta}\n[00:12.34]hola") == [(esperado, "hola")]


def test_parse_lrc_el_offset_no_deja_tiempos_negativos():
    assert lyrics.parse_lrc("[offset:+5000]\n[00:01.00]hola") == [(0, "hola")]


def test_parse_lrc_el_offset_vale_aunque_venga_despues_de_las_lineas():
    assert lyrics.parse_lrc("[00:10.00]hola\n[offset:+1000]") == [(9000, "hola")]


def test_parse_lrc_ordena_por_tiempo_y_respeta_el_orden_en_los_empates():
    assert lyrics.parse_lrc("[00:20.00]c\n[00:05.00]a\n[00:20.00]d\n[00:10.00]b") == [
        (5000, "a"), (10000, "b"), (20000, "c"), (20000, "d"),
    ]


def test_parse_lrc_ignora_lo_que_no_tiene_marca_y_las_lineas_vacias():
    assert lyrics.parse_lrc("\n\ncréditos sin marca\n[00:01.00]hola\n   \n[00:02.00]  chau  \n") == [
        (1000, "hola"), (2000, "chau"),
    ]


def test_parse_lrc_conserva_las_pausas_instrumentales_del_medio():
    assert lyrics.parse_lrc("[00:01.00]uno\n[00:05.00]\n[00:06.00]\n[00:09.00]dos") == [
        (1000, "uno"), (5000, ""), (9000, "dos"),
    ]


def test_parse_lrc_quita_las_pausas_del_principio_y_del_final():
    assert lyrics.parse_lrc("[00:00.00]\n[00:01.00]uno\n[00:05.00]\n[00:09.00]") == [(1000, "uno")]


@pytest.mark.parametrize("texto", ["", None, "solo texto\nsin marcas", "[ar:X]\n[ti:Y]", "[00:01.00]\n[00:02.00]"])
def test_parse_lrc_sin_lineas_con_tiempo_da_lista_vacia(texto):
    assert lyrics.parse_lrc(texto) == []


def test_parse_lrc_tolera_los_saltos_de_linea_de_windows():
    assert lyrics.parse_lrc("[00:01.00]uno\r\n[00:02.00]dos\r\n") == [(1000, "uno"), (2000, "dos")]


# --- letra sin tiempos y elección ------------------------------------------

def test_plain_letra_separa_en_lineas_y_marca_que_no_esta_sincronizada():
    assert lyrics.plain_letra("uno\ndos\n\ntres") == Letra(False, [(None, "uno"), (None, "dos"), (None, ""), (None, "tres")])


@pytest.mark.parametrize("texto", ["", "   \n\n ", None])
def test_plain_letra_vacia_es_none(texto):
    assert lyrics.plain_letra(texto) is None


def test_pick_letra_prefiere_la_sincronizada_aunque_venga_despues():
    resultados = [
        {"plainLyrics": "solo texto"},
        {"plainLyrics": "texto", "syncedLyrics": "[00:01.00]con tiempos"},
    ]
    assert lyrics.pick_letra(resultados) == Letra(True, [(1000, "con tiempos")])


def test_pick_letra_cae_al_texto_plano_si_nadie_trae_tiempos():
    resultados = [{"plainLyrics": ""}, {"syncedLyrics": "no es lrc"}, {"plainLyrics": "uno\ndos"}]
    assert lyrics.pick_letra(resultados) == Letra(False, [(None, "uno"), (None, "dos")])


def test_pick_letra_entre_varias_sincronizadas_gana_la_de_duracion_mas_cercana():
    resultados = [
        {"duration": 300, "syncedLyrics": "[00:01.00]directo"},
        {"duration": 212, "syncedLyrics": "[00:01.00]estudio"},
        {"syncedLyrics": "[00:01.00]sin duración"},
    ]
    assert lyrics.pick_letra(resultados, duracion_ms=213_000).lineas == [(1000, "estudio")]


def test_pick_letra_sin_duracion_conocida_gana_la_primera():
    resultados = [{"duration": 300, "syncedLyrics": "[00:01.00]primera"}, {"duration": 212, "syncedLyrics": "[00:01.00]otra"}]
    assert lyrics.pick_letra(resultados).lineas == [(1000, "primera")]


@pytest.mark.parametrize("resultados", [[], [{}], [{"plainLyrics": None, "syncedLyrics": None}], [{"instrumental": True}]])
def test_pick_letra_sin_nada_es_none(resultados):
    assert lyrics.pick_letra(resultados) is None


# --- artista y canción ------------------------------------------------------

def test_el_artista_sale_del_titulo_si_lo_trae():
    assert lyrics.artist_and_song("Soda Stereo - De Música Ligera (Official Video)", "SodaStereoVEVO") == ("Soda Stereo", "De Música Ligera")


@pytest.mark.parametrize("autor, artista", [("Rick Astley - Topic", "Rick Astley"), ("Rick Astley", "Rick Astley"), ("RickAstleyVEVO", "RickAstley"), ("", "")])
def test_sin_artista_en_el_titulo_se_usa_el_canal(autor, artista):
    assert lyrics.artist_and_song("Never Gonna Give You Up", autor) == (artista, "Never Gonna Give You Up")


# --- doble de la sesión de aiohttp -----------------------------------------

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


# --- find_synced ------------------------------------------------------------

async def test_find_synced_prefiere_syncedlyrics_y_la_devuelve_parseada():
    sesion = SesionFalsa(lrclib=lambda params: [CON_TIEMPOS])

    letra = await lyrics.find_synced(sesion, "Artista - Tema", "Artista", "Tema")

    assert letra == Letra(True, [(1000, "uno"), (2000, "dos")])


async def test_find_synced_usa_las_mismas_busquedas_que_find():
    vacia = SesionFalsa()
    await lyrics.find_synced(vacia, "Soda Stereo - De Música Ligera (Official Video)", "Soda Stereo", "De Música Ligera")
    otra = SesionFalsa()
    await lyrics.find(otra, "Soda Stereo - De Música Ligera (Official Video)", "Soda Stereo", "De Música Ligera")

    assert vacia.busquedas == otra.busquedas
    assert vacia.busquedas[:2] == [
        {"track_name": "De Música Ligera", "artist_name": "Soda Stereo"}, {"track_name": "De Música Ligera"},
    ]
    assert {"q": "Soda Stereo De Música Ligera"} in vacia.busquedas


async def test_find_synced_se_queda_con_la_primera_busqueda_que_da_algo():
    sesion = SesionFalsa(lrclib=lambda params: [CON_TIEMPOS] if "artist_name" in params else pytest.fail("siguió buscando"))

    await lyrics.find_synced(sesion, "A - T", "A", "T")

    assert len(sesion.busquedas) == 1


async def test_find_synced_sigue_con_la_siguiente_busqueda_si_la_primera_viene_vacia():
    sesion = SesionFalsa(lrclib=lambda params: [] if "artist_name" in params else [CON_TIEMPOS])

    letra = await lyrics.find_synced(sesion, "A - T", "A", "T")

    assert letra.sincronizada and len(sesion.busquedas) == 2


async def test_find_synced_cae_a_texto_plano_si_no_hay_tiempos():
    sesion = SesionFalsa(lrclib=lambda params: [{"plainLyrics": "uno\ndos"}])

    assert await lyrics.find_synced(sesion, "A - T", "A", "T") == Letra(False, [(None, "uno"), (None, "dos")])


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


async def test_find_synced_tolera_respuestas_que_no_son_una_lista():
    sesion = SesionFalsa(lrclib=lambda params: {"error": "mal"})

    assert await lyrics.find_synced(sesion, "Tema", "", "Tema") is None


# --- find sigue igual -------------------------------------------------------

async def test_find_devuelve_el_texto_plano_y_prefiere_plainlyrics():
    sesion = SesionFalsa(lrclib=lambda params: [{"plainLyrics": "texto plano", "syncedLyrics": "[00:01.00]otro"}])

    assert await lyrics.find(sesion, "A - T", "A", "T") == "texto plano"


async def test_find_sin_plain_usa_la_sincronizada_sin_las_marcas():
    sesion = SesionFalsa(lrclib=lambda params: [{"syncedLyrics": "[00:01.00]uno\n[00:02.50]dos"}])

    assert await lyrics.find(sesion, "A - T", "A", "T") == "uno\ndos"


async def test_find_salta_los_resultados_sin_letra():
    sesion = SesionFalsa(lrclib=lambda params: [{"plainLyrics": ""}, {}, {"plainLyrics": "la buena"}])

    assert await lyrics.find(sesion, "A - T", "A", "T") == "la buena"


async def test_find_cae_a_lyrics_ovh_y_sin_artista_no_pregunta():
    con = SesionFalsa(ovh=lambda artista, cancion: {"lyrics": "de ovh"})
    assert await lyrics.find(con, "Artista - Tema", "Artista", "Tema") == "de ovh"

    sin = SesionFalsa(ovh=lambda *_: pytest.fail("no debía llamar"))
    assert await lyrics.find(sin, "Tema", "", "Tema") is None


async def test_find_sin_nada_en_ningun_lado_es_none():
    assert await lyrics.find(SesionFalsa(), "A - T", "A", "T") is None


# --- caché ------------------------------------------------------------------

class Buscador:
    """`buscar` de juguete: cuenta las veces que se lo llama y contesta lo que se le diga."""

    def __init__(self, letra=Letra(True, [(0, "hola")]), demora=0.0):
        self.letra, self.demora, self.llamadas = letra, demora, 0

    async def __call__(self):
        self.llamadas += 1
        await asyncio.sleep(self.demora)
        return self.letra


class Reloj:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


async def test_la_cache_no_repite_la_busqueda_de_una_cancion():
    letras, buscar = lyrics.Letras(), Buscador()

    primera, segunda = await letras.obtener("abc", buscar), await letras.obtener("abc", buscar)

    assert primera == segunda == Letra(True, [(0, "hola")])
    assert buscar.llamadas == 1


async def test_la_cache_distingue_las_canciones():
    letras, buscar = lyrics.Letras(), Buscador()

    await letras.obtener("abc", buscar)
    await letras.obtener("xyz", buscar)

    assert buscar.llamadas == 2


async def test_la_cache_recuerda_tambien_el_no_encontrado():
    letras, buscar = lyrics.Letras(), Buscador(letra=None)

    assert await letras.obtener("abc", buscar) is None
    assert await letras.obtener("abc", buscar) is None
    assert buscar.llamadas == 1


async def test_el_no_encontrado_caduca_pero_la_letra_no():
    reloj = Reloj()
    letras = lyrics.Letras(caduca_vacia=60, reloj=reloj)
    sin, con = Buscador(letra=None), Buscador()
    await letras.obtener("sin", sin)
    await letras.obtener("con", con)

    reloj.t = 59
    await letras.obtener("sin", sin)
    assert sin.llamadas == 1

    reloj.t = 61
    await letras.obtener("sin", sin)
    await letras.obtener("con", con)
    assert (sin.llamadas, con.llamadas) == (2, 1)


async def test_la_cache_esta_acotada_y_saca_la_menos_usada():
    letras, buscar = lyrics.Letras(maximo=2), Buscador()
    await letras.obtener("a", buscar)
    await letras.obtener("b", buscar)
    await letras.obtener("a", buscar)      # `a` vuelve a ser la más reciente
    await letras.obtener("c", buscar)      # entra `c`, sale `b`
    assert buscar.llamadas == 3

    await letras.obtener("a", buscar)
    await letras.obtener("c", buscar)
    assert buscar.llamadas == 3

    await letras.obtener("b", buscar)
    assert buscar.llamadas == 4
    assert len(letras._datos) == 2


async def test_las_busquedas_simultaneas_de_la_misma_cancion_comparten_una():
    letras, buscar = lyrics.Letras(), Buscador(demora=0.01)

    resultados = await asyncio.gather(*(letras.obtener("abc", buscar) for _ in range(4)))

    assert buscar.llamadas == 1
    assert all(r == Letra(True, [(0, "hola")]) for r in resultados)


async def test_si_quien_pregunta_se_cancela_la_busqueda_termina_y_queda_en_cache():
    letras, buscar = lyrics.Letras(), Buscador(demora=0.02)

    tarea = asyncio.ensure_future(letras.obtener("abc", buscar))
    await asyncio.sleep(0)
    tarea.cancel()
    await asyncio.sleep(0.05)

    assert await letras.obtener("abc", buscar) == Letra(True, [(0, "hola")])
    assert buscar.llamadas == 1


async def test_una_busqueda_que_se_pasa_del_tiempo_cuenta_como_no_encontrada(monkeypatch):
    monkeypatch.setattr(lyrics, "TOTAL_TIMEOUT", 0.01)
    letras, buscar = lyrics.Letras(), Buscador(demora=1)

    assert await letras.obtener("abc", buscar) is None
    assert await letras.obtener("abc", buscar) is None
    assert buscar.llamadas == 1


async def test_un_error_inesperado_sale_y_no_se_guarda():
    letras = lyrics.Letras()
    llamadas = []

    async def buscar():
        llamadas.append(1)
        raise RuntimeError("bug")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await letras.obtener("abc", buscar)
    assert len(llamadas) == 2 and not letras._en_vuelo
