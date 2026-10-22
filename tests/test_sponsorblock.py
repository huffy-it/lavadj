"""SponsorBlock: qué se le pregunta al servidor, qué se hace con la respuesta y que nada de esto frene la música.

El servidor es uno de juguete en localhost: ningún test sale a la red.
"""

import asyncio
import hashlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core import config, sponsorblock
from core.sponsorblock import Segmento, SponsorBlock, fin_de_intro, llega_al_final, utiles


class Servidor:
    """Un SponsorBlock de mentira: anota qué le preguntan y responde lo que se le diga."""

    def __init__(self):
        self.peticiones = []
        self.estado = 200
        self.cuerpo = []
        self.espera = 0.0
        self.crudo = None
        self.url = ""

    async def atender(self, request):
        self.peticiones.append((request.match_info["prefijo"], request.query.get("categories")))
        if self.espera:
            await asyncio.sleep(self.espera)
        if self.crudo is not None:
            return web.Response(text=self.crudo, content_type="text/plain", status=self.estado)
        return web.json_response(self.cuerpo, status=self.estado)


@pytest.fixture
async def servidor():
    falso = Servidor()
    app = web.Application()
    app.router.add_get("/api/skipSegments/{prefijo}", falso.atender)
    real = TestServer(app)
    await real.start_server()
    falso.url = str(real.make_url("/api/skipSegments"))
    yield falso
    await real.close()


@pytest.fixture
async def hacer(servidor):
    """Fábrica de `SponsorBlock` apuntando al servidor de juguete; los cierra al terminar."""
    creados = []

    def _hacer(**kwargs):
        kwargs.setdefault("url", servidor.url)
        kwargs.setdefault("activo", True)
        creados.append(SponsorBlock(**kwargs))
        return creados[-1]

    yield _hacer
    for sb in creados:
        await sb.cerrar()


def video(id_, *partes, duracion=200):
    """La entrada de un video en la respuesta por prefijo; cada parte es (inicio, fin) en segundos."""
    return {
        "videoID": id_,
        "segments": [
            {"category": "music_offtopic", "actionType": "skip", "segment": [a, b], "UUID": f"{id_}-{a}",
             "videoDuration": duracion, "locked": 0, "votes": 0, "description": ""}
            for a, b in partes
        ],
    }


# --- la consulta --------------------------------------------------------------

async def test_pregunta_con_el_prefijo_del_hash_y_la_categoria(servidor, hacer):
    await hacer().segmentos("dQw4w9WgXcQ")

    prefijo, categorias = servidor.peticiones[0]
    assert prefijo == hashlib.sha256(b"dQw4w9WgXcQ").hexdigest()[:4]
    assert len(prefijo) == 4
    assert categorias == '["music_offtopic"]'


async def test_las_categorias_son_las_configuradas(servidor, hacer):
    await hacer(categorias=("music_offtopic", "intro")).segmentos("abc")

    assert servidor.peticiones[0][1] == '["music_offtopic", "intro"]'


async def test_filtra_la_respuesta_por_el_video_exacto(servidor, hacer):
    """El prefijo trae también a otros videos: solo valen los del pedido."""
    servidor.cuerpo = [video("otro", (1, 2)), video("abc", (0, 12.5), (100, 130.25))]

    assert await hacer().segmentos("abc") == (Segmento(0, 12500, 200_000), Segmento(100_000, 130_250, 200_000))


async def test_si_el_video_no_viene_en_la_respuesta_no_hay_segmentos(servidor, hacer):
    servidor.cuerpo = [video("otro", (1, 2))]

    assert await hacer().segmentos("abc") == ()


async def test_un_404_es_sin_segmentos_y_no_es_un_error(servidor, hacer, caplog):
    servidor.estado = 404
    sb = hacer()

    with caplog.at_level("WARNING", logger="sponsorblock"):
        assert await sb.segmentos("abc") == ()

    assert caplog.text == ""
    assert sb.en_cache("abc") == ()


async def test_un_error_del_servidor_da_vacio_avisa_y_no_lanza(servidor, hacer, caplog):
    servidor.estado = 500
    sb = hacer()

    with caplog.at_level("WARNING", logger="sponsorblock"):
        assert await sb.segmentos("abc") == ()

    assert "SponsorBlock no respondió" in caplog.text
    assert sb.en_cache("abc") is None


async def test_un_servidor_lento_da_vacio_al_pasarse_del_tiempo_limite(servidor, hacer, caplog):
    servidor.espera = 1.0
    sb = hacer(timeout=0.05)

    with caplog.at_level("WARNING", logger="sponsorblock"):
        assert await sb.segmentos("abc") == ()

    assert "SponsorBlock no respondió" in caplog.text


async def test_sin_servidor_da_vacio_y_no_lanza(servidor, hacer, caplog):
    sb = hacer(url="http://127.0.0.1:9/api/skipSegments", timeout=0.3)

    with caplog.at_level("WARNING", logger="sponsorblock"):
        assert await sb.segmentos("abc") == ()

    assert "SponsorBlock no respondió" in caplog.text


@pytest.mark.parametrize("cuerpo", [
    {"no": "es una lista"},
    [None, "texto", 3, {"videoID": "abc", "segments": "nada"}],
    [{"videoID": "abc", "segments": [{"segment": [5]}, {"segment": "x"}, {"sin": "segmento"}, None, 7]}],
    [{"videoID": "abc", "segments": [{"segment": [10, 5]}, {"segment": [-3, 4]}, {"segment": [1, float("inf")]}]}],
])
async def test_una_respuesta_mal_formada_no_rompe_nada(servidor, hacer, cuerpo):
    servidor.cuerpo = cuerpo

    assert await hacer().segmentos("abc") == ()


async def test_solo_valen_los_segmentos_de_saltar(servidor, hacer):
    entrada = video("abc", (0, 10), (50, 60))
    entrada["segments"][1]["actionType"] = "mute"
    servidor.cuerpo = [entrada]

    assert await hacer().segmentos("abc") == (Segmento(0, 10_000, 200_000),)


async def test_los_segmentos_salen_ordenados_y_sin_solaparse(servidor, hacer):
    servidor.cuerpo = [video("abc", (100, 120), (0, 10), (5, 15), (118, 125))]

    assert [(s.inicio, s.fin) for s in await hacer().segmentos("abc")] == [(0, 15_000), (100_000, 125_000)]


async def test_la_respuesta_sin_content_type_json_tambien_se_lee(servidor, hacer):
    servidor.crudo = '[{"videoID": "abc", "segments": [{"segment": [0, 3]}]}]'

    assert await hacer().segmentos("abc") == (Segmento(0, 3000, 0),)


# --- la caché ---------------------------------------------------------------------

async def test_la_segunda_vez_sale_de_la_cache_sin_preguntar(servidor, hacer):
    servidor.cuerpo = [video("abc", (0, 10))]
    sb = hacer()

    primera = await sb.segmentos("abc")
    segunda = await sb.segmentos("abc")

    assert primera == segunda == (Segmento(0, 10_000, 200_000),)
    assert len(servidor.peticiones) == 1


async def test_la_cache_recuerda_tambien_los_videos_sin_segmentos(servidor, hacer):
    sb = hacer()

    await sb.segmentos("abc")
    await sb.segmentos("abc")

    assert sb.en_cache("abc") == ()
    assert len(servidor.peticiones) == 1


async def test_leer_la_cache_no_espera_ni_pregunta(servidor, hacer):
    sb = hacer()

    assert sb.en_cache("abc") is None
    assert servidor.peticiones == []


async def test_la_cache_tiene_tope_y_olvida_el_menos_reciente(servidor, hacer):
    sb = hacer(tope=2)

    await sb.segmentos("uno")
    await sb.segmentos("dos")
    sb.en_cache("uno")
    await sb.segmentos("tres")

    assert sb.en_cache("dos") is None
    assert sb.en_cache("uno") == () and sb.en_cache("tres") == ()


async def test_dos_pedidos_a_la_vez_hacen_una_sola_consulta(servidor, hacer):
    servidor.cuerpo = [video("abc", (0, 10))]
    sb = hacer()

    uno, otro = await asyncio.gather(sb.segmentos("abc"), sb.segmentos("abc"))

    assert uno == otro == (Segmento(0, 10_000, 200_000),)
    assert len(servidor.peticiones) == 1


async def test_precargar_deja_la_respuesta_en_la_cache_y_pedirla_se_suma_a_la_consulta(servidor, hacer):
    servidor.cuerpo = [video("abc", (0, 10))]
    servidor.espera = 0.05
    sb = hacer()

    sb.precargar("abc")
    assert sb.en_cache("abc") is None
    segmentos = await sb.segmentos("abc")

    assert segmentos == sb.en_cache("abc") == (Segmento(0, 10_000, 200_000),)
    assert len(servidor.peticiones) == 1


async def test_si_quien_espera_se_cancela_la_consulta_sigue_y_llena_la_cache(servidor, hacer):
    servidor.espera = 0.05
    sb = hacer()

    espera = asyncio.create_task(sb.segmentos("abc"))
    await asyncio.sleep(0.01)
    espera.cancel()
    await asyncio.gather(espera, return_exceptions=True)
    await asyncio.sleep(0.2)

    assert sb.en_cache("abc") == ()


# --- fallos: no se insiste ---------------------------------------------------------

async def test_tras_un_fallo_no_vuelve_a_preguntar_por_un_rato(servidor, hacer):
    marca = {"t": 100.0}
    servidor.estado = 500
    sb = hacer(reloj=lambda: marca["t"])

    await sb.segmentos("uno")
    marca["t"] += sponsorblock.PAUSA_TRAS_ERROR - 1
    assert await sb.segmentos("dos") == ()
    assert len(servidor.peticiones) == 1

    marca["t"] += 2
    servidor.estado = 200
    await sb.segmentos("dos")
    assert len(servidor.peticiones) == 2
    assert sb.en_cache("dos") == ()


# --- apagado y cierre ---------------------------------------------------------------

async def test_con_sponsorblock_apagado_no_consulta_nada(servidor, hacer):
    sb = hacer(activo=False)

    sb.precargar("abc")
    assert await sb.segmentos("abc") == ()
    await sb.cerrar()

    assert servidor.peticiones == []
    assert sb.en_cache("abc") is None


async def test_un_video_sin_id_no_consulta_nada(servidor, hacer):
    assert await hacer().segmentos("") == ()
    assert servidor.peticiones == []


def test_sin_argumentos_toma_la_configuracion_al_construirse(monkeypatch):
    monkeypatch.setattr(config, "SPONSORBLOCK", True)
    monkeypatch.setattr(config, "SPONSORBLOCK_CATEGORIAS", ("intro",))

    sb = SponsorBlock()

    assert sb.activo is True and sb.categorias == ("intro",)


def test_por_defecto_esta_apagado_en_los_tests_y_la_categoria_es_music_offtopic():
    assert SponsorBlock().activo is False
    assert config.SPONSORBLOCK_CATEGORIAS == ("music_offtopic",)


async def test_la_sesion_nace_al_primer_uso_y_se_cierra_y_se_reabre(servidor, hacer):
    sb = hacer()
    assert sb._http is None

    await sb.segmentos("uno")
    primera = sb._http
    assert primera is not None and not primera.closed

    await sb.cerrar()
    assert primera.closed and sb._http is None

    await sb.segmentos("dos")
    assert sb._http is not None and sb._http is not primera
    assert len(servidor.peticiones) == 2


async def test_cerrar_corta_las_consultas_en_curso(servidor, hacer):
    servidor.espera = 5.0
    sb = hacer(timeout=10)

    sb.precargar("abc")
    await asyncio.sleep(0.05)
    await asyncio.wait_for(sb.cerrar(), timeout=2)

    assert sb._en_curso == {}
    assert sb.en_cache("abc") is None


# --- qué segmentos valen para una pista ---------------------------------------------------

def test_utiles_descarta_los_de_otra_edicion_del_video():
    mismo = Segmento(0, 10_000, 200_000)
    desfasado = Segmento(50_000, 60_000, 230_000)
    sin_dato = Segmento(70_000, 80_000, 0)

    assert utiles([mismo, desfasado, sin_dato], 201_000) == (mismo, sin_dato)


def test_utiles_descarta_el_que_se_come_la_cancion_entera():
    entero = Segmento(0, 199_000, 200_000)
    intro = Segmento(0, 10_000, 200_000)
    final = Segmento(150_000, 200_000, 200_000)

    assert utiles([entero], 200_000) == ()
    assert utiles([intro, final], 200_000) == (intro, final)


def test_utiles_sin_largo_conocido_no_descarta_nada():
    segmentos = (Segmento(0, 199_000, 200_000), Segmento(5, 9, 1))

    assert utiles(segmentos, 0) == segmentos


def test_la_intro_es_el_segmento_que_empieza_en_el_primer_segundo():
    assert fin_de_intro([Segmento(0, 12_500), Segmento(100_000, 120_000)]) == 12_500
    assert fin_de_intro([Segmento(1000, 9000)]) == 9000
    assert fin_de_intro([Segmento(1001, 9000)]) == 0
    assert fin_de_intro([]) == 0


def test_llega_al_final_si_termina_a_unos_segundos_del_largo():
    assert llega_al_final(Segmento(150_000, 196_000), 200_000)
    assert llega_al_final(Segmento(150_000, 200_000), 200_000)
    assert not llega_al_final(Segmento(150_000, 190_000), 200_000)
    assert not llega_al_final(Segmento(150_000, 190_000), 0)
