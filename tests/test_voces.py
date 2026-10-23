"""Las voces de Piper: catálogo, instaladas, descarga verificada, elegir y borrar; sin red salvo `_bajar` contra un servidor local."""

import asyncio
import hashlib
import re

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core import ajustes, config, voces
from core.voces import CATALOGO, ErrorDeVoz, Voz

MODELO = b"modelo" * 1000
CONFIG = b'{"audio": {}}'
PRUEBA = "es_MX-prueba-medium"
ALD = "es_MX-ald-medium"


def md5(datos: bytes) -> str:
    return hashlib.md5(datos).hexdigest()


@pytest.fixture
def catalogo(monkeypatch):
    """Un catálogo con una voz de mentira cuyos md5 coinciden con MODELO y CONFIG."""
    voz = Voz(PRUEBA, len(MODELO), md5(MODELO), md5(CONFIG))
    monkeypatch.setattr(voces, "CATALOGO", {**CATALOGO, PRUEBA: voz})
    return voz


@pytest.fixture
def descargas(monkeypatch):
    nuevas = voces.Descargas()
    monkeypatch.setattr(voces, "descargas", nuevas)
    return nuevas


class Red:
    """Doble de `_bajar`: sirve bytes por URL, cuenta las bajadas y puede frenarse a mitad del modelo."""

    def __init__(self, monkeypatch):
        self.llamadas: list[str] = []
        self.modelo, self.config = MODELO, CONFIG
        self.puerta = asyncio.Event()
        self.puerta.set()
        self.fallo: Exception | None = None
        monkeypatch.setattr(voces, "_bajar", self.bajar)

    async def bajar(self, url, destino, al_avanzar=None):
        self.llamadas.append(url)
        if self.fallo:
            raise self.fallo
        datos = self.modelo if url.endswith(".onnx") else self.config
        destino.write_bytes(datos)
        if al_avanzar:
            al_avanzar(len(datos) // 2)
            await self.puerta.wait()
            al_avanzar(len(datos))


@pytest.fixture
def red(monkeypatch):
    return Red(monkeypatch)


async def terminar(descargas, voz_id=PRUEBA):
    async with asyncio.timeout(5):
        while descargas.activa(voz_id):
            await asyncio.sleep(0.005)


async def frenar_a_mitad_del_modelo(descargas, red, voz_id=PRUEBA):
    red.puerta.clear()
    descargas.iniciar(voz_id)
    async with asyncio.timeout(5):
        while descargas.progreso(voz_id) != 50:
            await asyncio.sleep(0.005)


def instalar(voz_id):
    config.CARPETA_DE_VOCES.mkdir(parents=True, exist_ok=True)
    voces.ruta_modelo(voz_id).write_bytes(b"onnx")
    voces.ruta_config(voz_id).write_bytes(b"{}")


def test_el_catalogo_trae_espeak_y_las_voces_en_espanol_con_tamano_y_md5():
    assert voces.ESPEAK in CATALOGO and not CATALOGO[voces.ESPEAK].es_piper
    piper = [v for v in CATALOGO.values() if v.es_piper]
    assert {v.pais for v in piper} == {"Argentina", "España", "México"}
    assert {v.calidad for v in piper} >= {"x_low", "low", "medium", "high"}
    assert all(re.fullmatch(r"es_(AR|ES|MX)-\w+-(x_low|low|medium|high)", v.id) for v in piper)
    assert all(v.tamano > 10_000_000 for v in piper)
    assert all(re.fullmatch(r"[0-9a-f]{32}", m) for v in piper for m in (v.md5_modelo, v.md5_config))


def test_las_urls_van_fijas_al_tag_salvo_la_config_corregida_de_ald():
    raiz = "https://huggingface.co/rhasspy/piper-voices/resolve/"
    assert CATALOGO["es_MX-claude-high"].url_modelo == raiz + "v1.0.0/es/es_MX/claude/high/es_MX-claude-high.onnx"
    assert CATALOGO["es_ES-carlfm-x_low"].url_config == raiz + "v1.0.0/es/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx.json"
    ald = CATALOGO[ALD]
    assert ald.url_modelo.startswith(raiz + "v1.0.0/")
    assert ald.url_config == raiz + voces.REV_ALD_CONFIG + "/es/es_MX/ald/medium/es_MX-ald-medium.onnx.json"
    assert all(v.url_modelo.startswith(raiz) and v.url_config.startswith(raiz) for v in CATALOGO.values() if v.es_piper)


def test_sin_descargar_nada_solo_esta_espeak_y_lo_que_no_es_del_catalogo_no_cuenta():
    instalar("es_MX-fantasma-high")
    assert voces.instaladas() == []
    assert voces.instalada(voces.ESPEAK)
    assert not voces.instalada(ALD) and not voces.instalada("es_MX-fantasma-high")


def test_instalada_pide_los_dos_archivos_de_la_carpeta_de_voces():
    assert voces.ruta_modelo(ALD) == config.CARPETA_DE_VOCES / "es_MX-ald-medium.onnx"
    assert voces.ruta_config(ALD) == config.CARPETA_DE_VOCES / "es_MX-ald-medium.onnx.json"
    config.CARPETA_DE_VOCES.mkdir(parents=True)
    voces.ruta_modelo(ALD).write_bytes(b"onnx")
    assert not voces.instalada(ALD)
    voces.ruta_config(ALD).write_bytes(b"{}")
    assert voces.instalada(ALD)
    assert voces.instaladas() == [ALD]


def test_piper_disponible_busca_el_paquete_sin_importarlo(monkeypatch):
    buscado = []
    monkeypatch.setattr(voces.importlib.util, "find_spec", lambda nombre: buscado.append(nombre) or None)
    assert not voces.piper_disponible()
    monkeypatch.setattr(voces.importlib.util, "find_spec", lambda nombre: object())
    assert voces.piper_disponible()
    assert buscado == ["piper"]


async def test_la_descarga_instala_la_voz_verificada(catalogo, descargas, red):
    descargas.iniciar(PRUEBA)
    assert descargas.activa(PRUEBA)
    await terminar(descargas)
    assert voces.instalada(PRUEBA)
    assert voces.ruta_modelo(PRUEBA).read_bytes() == MODELO
    assert voces.ruta_config(PRUEBA).read_bytes() == CONFIG
    assert descargas.progreso(PRUEBA) == 100 and descargas.error(PRUEBA) is None
    assert sorted(p.name for p in config.CARPETA_DE_VOCES.iterdir()) == [f"{PRUEBA}.onnx", f"{PRUEBA}.onnx.json"]
    assert red.llamadas == [catalogo.url_config, catalogo.url_modelo]


async def test_mientras_baja_se_ve_el_progreso_y_no_se_baja_otra_vez_ni_se_borra(catalogo, descargas, red):
    await frenar_a_mitad_del_modelo(descargas, red)
    assert descargas.activa(PRUEBA) and not voces.instalada(PRUEBA)
    voz = next(v for v in voces.listar() if v["id"] == PRUEBA)
    assert voz["descargando"] and voz["progreso"] == 50
    with pytest.raises(ErrorDeVoz, match="ya se está descargando"):
        descargas.iniciar(PRUEBA)
    with pytest.raises(ErrorDeVoz, match="descargando"):
        voces.borrar(PRUEBA)
    red.puerta.set()
    await terminar(descargas)
    assert descargas.progreso(PRUEBA) == 100       # no llega a 100 hasta instalar
    assert red.llamadas.count(catalogo.url_modelo) == 1


@pytest.mark.parametrize("que, dato, bajadas", [
    ("modelo", b"otra cosa", ["url_config", "url_modelo"]),
    ("config", b"{}", ["url_config"]),
])
async def test_un_md5_que_no_coincide_borra_los_temporales_y_no_instala(catalogo, descargas, red, que, dato, bajadas):
    setattr(red, que, dato)
    descargas.iniciar(PRUEBA)
    await terminar(descargas)
    assert not voces.instalada(PRUEBA)
    assert "md5" in descargas.error(PRUEBA)
    assert red.llamadas == [getattr(catalogo, url) for url in bajadas]
    assert list(config.CARPETA_DE_VOCES.iterdir()) == []         # ni .part ni la config a medias


async def test_un_error_de_red_queda_como_error_y_se_puede_reintentar(catalogo, descargas, red):
    red.fallo = aiohttp.ClientConnectionError("sin red")
    descargas.iniciar(PRUEBA)
    await terminar(descargas)
    assert descargas.error(PRUEBA) and not voces.instalada(PRUEBA)
    red.fallo = None
    descargas.iniciar(PRUEBA)
    assert descargas.error(PRUEBA) is None            # el reintento limpia el error anterior
    await terminar(descargas)
    assert voces.instalada(PRUEBA)


async def test_dos_voces_distintas_pueden_bajar_a_la_vez(catalogo, descargas, red, monkeypatch):
    red.puerta.clear()
    otra = Voz("es_ES-otra-low", len(MODELO), md5(MODELO), md5(CONFIG))
    monkeypatch.setattr(voces, "CATALOGO", {**voces.CATALOGO, otra.id: otra})
    descargas.iniciar(PRUEBA)
    descargas.iniciar(otra.id)
    assert descargas.activa(PRUEBA) and descargas.activa(otra.id)
    red.puerta.set()
    await terminar(descargas)
    await terminar(descargas, otra.id)
    assert voces.instalada(PRUEBA) and voces.instalada(otra.id)


async def test_cancelar_la_descarga_no_deja_temporales(catalogo, descargas, red):
    await frenar_a_mitad_del_modelo(descargas, red)
    descargas._tareas[PRUEBA].cancel()
    await terminar(descargas)
    assert not descargas.activa(PRUEBA)
    assert list(config.CARPETA_DE_VOCES.iterdir()) == []


@pytest.mark.parametrize("voz_id", ["https://evil.example/x.onnx", "../fuera", "es_MX-fantasma-high", voces.ESPEAK])
async def test_solo_se_baja_lo_que_esta_en_el_catalogo(descargas, red, voz_id):
    with pytest.raises(ErrorDeVoz):
        descargas.iniciar(voz_id)
    assert red.llamadas == [] and not descargas.activa(voz_id)


async def test_no_baja_una_voz_ya_instalada(descargas, red):
    instalar(ALD)
    with pytest.raises(ErrorDeVoz, match="ya está instalada"):
        descargas.iniciar(ALD)
    assert red.llamadas == []


async def test_bajar_escribe_el_archivo_y_avisa_el_progreso(tmp_path):
    async def datos(_):
        return web.Response(body=MODELO)
    app = web.Application()
    app.router.add_get("/m.onnx", datos)
    avances = []
    async with TestServer(app) as servidor:
        await voces._bajar(str(servidor.make_url("/m.onnx")), tmp_path / "m.onnx", avances.append)
    assert (tmp_path / "m.onnx").read_bytes() == MODELO
    assert avances[-1] == len(MODELO) and avances == sorted(avances)


async def test_bajar_con_error_http_levanta(tmp_path):
    async with TestServer(web.Application()) as servidor:
        with pytest.raises(aiohttp.ClientResponseError):
            await voces._bajar(str(servidor.make_url("/no-existe")), tmp_path / "x", None)


@pytest.mark.parametrize("voz_id, hay_piper", [(ALD, True), (voces.ESPEAK, False)])
def test_elegir_guarda_una_voz_instalada_y_espeak_siempre_se_puede(monkeypatch, voz_id, hay_piper):
    monkeypatch.setattr(voces, "piper_disponible", lambda: hay_piper)
    instalar(ALD)
    ajustes.guardar(voz="es_ES-davefx-medium")
    voces.elegir(voz_id)
    assert ajustes.obtener("voz") == voz_id


@pytest.mark.parametrize("voz_id, hay_piper, error", [
    (ALD, False, "no está instalada"),
    ("es_MX-fantasma-high", True, "no está instalada"),
    (ALD, False, "piper-tts"),
])
def test_elegir_lo_que_no_se_puede_es_error_y_no_cambia_nada(monkeypatch, voz_id, hay_piper, error):
    monkeypatch.setattr(voces, "piper_disponible", lambda: hay_piper)
    if error == "piper-tts":
        instalar(voz_id)
    with pytest.raises(ErrorDeVoz, match=error):
        voces.elegir(voz_id)
    assert ajustes.obtener("voz") == voces.ESPEAK


def test_si_no_se_puede_guardar_el_ajuste_es_un_error_de_voz(monkeypatch):
    def falla(**_):
        raise OSError("disco lleno")
    monkeypatch.setattr(ajustes, "guardar", falla)
    with pytest.raises(ErrorDeVoz, match="disco lleno"):
        voces.poner_filtro(False)


def test_el_filtro_de_radio_se_enciende_y_se_apaga():
    voces.poner_filtro(False)
    assert ajustes.obtener("filtro_radio") is False
    voces.poner_filtro(True)
    assert ajustes.obtener("filtro_radio") is True


def test_borrar_quita_los_dos_archivos():
    instalar(ALD)
    voces.borrar(ALD)
    assert not voces.instalada(ALD)
    assert list(config.CARPETA_DE_VOCES.iterdir()) == []


def test_no_se_borra_la_voz_en_uso():
    instalar(ALD)
    ajustes.guardar(voz=ALD)
    with pytest.raises(ErrorDeVoz, match="en uso"):
        voces.borrar(ALD)
    assert voces.instalada(ALD)


@pytest.mark.parametrize("voz_id", [voces.ESPEAK, "es_MX-fantasma-high", "../../x"])
def test_no_se_borra_lo_que_no_es_una_voz_de_piper(voz_id):
    with pytest.raises(ErrorDeVoz):
        voces.borrar(voz_id)


def test_listar_da_el_estado_de_cada_voz(descargas):
    instalar(ALD)
    instalar("es_ES-davefx-medium")
    ajustes.guardar(voz="es_ES-davefx-medium")
    descargas._error["es_MX-claude-high"] = "sin red"
    lista = {v["id"]: v for v in voces.listar()}
    assert list(lista) == list(CATALOGO)
    assert lista["es_ES-davefx-medium"]["en_uso"] and lista["es_ES-davefx-medium"]["instalada"]
    assert lista[ALD]["instalada"] and not lista[ALD]["en_uso"]
    assert lista[ALD]["pais"] == "México" and lista[ALD]["calidad"] == "medium"
    assert lista["es_MX-claude-high"]["error"] == "sin red" and not lista["es_MX-claude-high"]["instalada"]
    assert not lista["es_AR-daniela-high"]["instalada"] and not lista["es_AR-daniela-high"]["descargando"]
    assert lista[voces.ESPEAK]["instalada"] and not lista[voces.ESPEAK]["en_uso"]
