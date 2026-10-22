"""Las voces de Piper: catálogo, voces instaladas, descarga verificada, elegir y borrar.

Ninguna prueba baja nada de internet: `_bajar` se reemplaza por un doble que escribe
bytes conocidos (salvo las dos que lo prueban contra un servidor local).
"""

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

    def __init__(self, monkeypatch, modelo=MODELO, config_=CONFIG):
        self.llamadas: list[str] = []
        self.modelo, self.config = modelo, config_
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


async def terminar(descargas, voz_id):
    """Espera a que la descarga en segundo plano acabe."""
    async with asyncio.timeout(5):
        while descargas.activa(voz_id):
            await asyncio.sleep(0.005)


async def a_mitad_del_modelo(descargas, voz_id):
    """Espera a que la descarga llegue a la mitad del modelo (el doble queda frenado ahí)."""
    async with asyncio.timeout(5):
        while descargas.progreso(voz_id) != 50:
            await asyncio.sleep(0.005)


def instalar(voz_id):
    config.CARPETA_DE_VOCES.mkdir(parents=True, exist_ok=True)
    voces.ruta_modelo(voz_id).write_bytes(b"onnx")
    voces.ruta_config(voz_id).write_bytes(b"{}")


# --- catálogo ---------------------------------------------------------------

def test_el_catalogo_trae_espeak_y_las_voces_en_espanol_de_los_tres_paises():
    assert voces.ESPEAK in CATALOGO and not CATALOGO[voces.ESPEAK].es_piper
    piper = [v for v in CATALOGO.values() if v.es_piper]
    assert {v.pais for v in piper} == {"Argentina", "España", "México"}
    assert {v.calidad for v in piper} >= {"x_low", "low", "medium", "high"}
    assert all(re.fullmatch(r"es_(AR|ES|MX)-\w+-(x_low|low|medium|high)", v.id) for v in piper)


def test_cada_voz_de_piper_trae_tamano_y_los_dos_md5():
    for voz in CATALOGO.values():
        if voz.es_piper:
            assert voz.tamano > 10_000_000
            assert re.fullmatch(r"[0-9a-f]{32}", voz.md5_modelo) and re.fullmatch(r"[0-9a-f]{32}", voz.md5_config)


def test_las_urls_van_fijas_al_tag_salvo_la_config_corregida_de_ald():
    raiz = "https://huggingface.co/rhasspy/piper-voices/resolve/"
    assert CATALOGO["es_MX-claude-high"].url_modelo == raiz + "v1.0.0/es/es_MX/claude/high/es_MX-claude-high.onnx"
    assert CATALOGO["es_ES-carlfm-x_low"].url_config == raiz + "v1.0.0/es/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx.json"
    ald = CATALOGO["es_MX-ald-medium"]
    assert ald.url_modelo.startswith(raiz + "v1.0.0/")
    assert ald.url_config == raiz + voces.REV_ALD_CONFIG + "/es/es_MX/ald/medium/es_MX-ald-medium.onnx.json"
    for voz in CATALOGO.values():
        if voz.es_piper:
            assert voz.url_modelo.startswith(raiz) and voz.url_config.startswith(raiz)


def test_los_archivos_van_a_la_carpeta_de_voces():
    assert voces.ruta_modelo("es_MX-ald-medium") == config.CARPETA_DE_VOCES / "es_MX-ald-medium.onnx"
    assert voces.ruta_config("es_MX-ald-medium") == config.CARPETA_DE_VOCES / "es_MX-ald-medium.onnx.json"


# --- instaladas -------------------------------------------------------------

def test_sin_descargar_nada_solo_esta_espeak():
    assert voces.instaladas() == []
    assert voces.instalada(voces.ESPEAK)
    assert not voces.instalada("es_MX-ald-medium")


def test_instalada_pide_los_dos_archivos():
    config.CARPETA_DE_VOCES.mkdir(parents=True)
    voces.ruta_modelo("es_MX-ald-medium").write_bytes(b"onnx")
    assert not voces.instalada("es_MX-ald-medium")

    voces.ruta_config("es_MX-ald-medium").write_bytes(b"{}")
    assert voces.instalada("es_MX-ald-medium")
    assert voces.instaladas() == ["es_MX-ald-medium"]


def test_una_voz_que_no_esta_en_el_catalogo_no_cuenta_como_instalada():
    instalar("es_MX-fantasma-high")
    assert not voces.instalada("es_MX-fantasma-high")
    assert voces.instaladas() == []


def test_piper_disponible_busca_el_paquete_sin_importarlo(monkeypatch):
    buscado = []
    monkeypatch.setattr(voces.importlib.util, "find_spec", lambda nombre: buscado.append(nombre) or None)
    assert not voces.piper_disponible()
    monkeypatch.setattr(voces.importlib.util, "find_spec", lambda nombre: object())
    assert voces.piper_disponible()
    assert buscado == ["piper"]


# --- descarga ---------------------------------------------------------------

async def test_la_descarga_instala_la_voz_verificada(catalogo, descargas, monkeypatch):
    red = Red(monkeypatch)

    descargas.iniciar(PRUEBA)
    assert descargas.activa(PRUEBA)
    await terminar(descargas, PRUEBA)

    assert voces.instalada(PRUEBA)
    assert voces.ruta_modelo(PRUEBA).read_bytes() == MODELO
    assert voces.ruta_config(PRUEBA).read_bytes() == CONFIG
    assert descargas.progreso(PRUEBA) == 100 and descargas.error(PRUEBA) is None
    assert sorted(p.name for p in config.CARPETA_DE_VOCES.iterdir()) == [f"{PRUEBA}.onnx", f"{PRUEBA}.onnx.json"]
    assert red.llamadas == [catalogo.url_config, catalogo.url_modelo]


async def test_el_progreso_se_expone_mientras_baja_y_no_llega_a_100_hasta_instalar(catalogo, descargas, monkeypatch):
    red = Red(monkeypatch)
    red.puerta.clear()

    descargas.iniciar(PRUEBA)
    await a_mitad_del_modelo(descargas, PRUEBA)
    assert descargas.activa(PRUEBA) and not voces.instalada(PRUEBA)

    red.puerta.set()
    await terminar(descargas, PRUEBA)
    assert descargas.progreso(PRUEBA) == 100


async def test_un_md5_que_no_coincide_borra_el_temporal_y_no_instala(catalogo, descargas, monkeypatch):
    Red(monkeypatch, modelo=b"otra cosa")

    descargas.iniciar(PRUEBA)
    await terminar(descargas, PRUEBA)

    assert not voces.instalada(PRUEBA)
    assert "md5" in descargas.error(PRUEBA)
    assert list(config.CARPETA_DE_VOCES.iterdir()) == []         # ni .part ni la config a medias


async def test_un_md5_malo_en_la_config_corta_antes_de_bajar_el_modelo(catalogo, descargas, monkeypatch):
    red = Red(monkeypatch, config_=b"{}")

    descargas.iniciar(PRUEBA)
    await terminar(descargas, PRUEBA)

    assert "md5" in descargas.error(PRUEBA)
    assert red.llamadas == [catalogo.url_config]
    assert list(config.CARPETA_DE_VOCES.iterdir()) == []


async def test_un_error_de_red_queda_como_error_y_se_puede_reintentar(catalogo, descargas, monkeypatch):
    red = Red(monkeypatch)
    red.fallo = aiohttp.ClientConnectionError("sin red")

    descargas.iniciar(PRUEBA)
    await terminar(descargas, PRUEBA)
    assert descargas.error(PRUEBA) and not voces.instalada(PRUEBA)

    red.fallo = None
    descargas.iniciar(PRUEBA)
    assert descargas.error(PRUEBA) is None            # el reintento limpia el error anterior
    await terminar(descargas, PRUEBA)
    assert voces.instalada(PRUEBA)


async def test_una_sola_descarga_por_voz_a_la_vez(catalogo, descargas, monkeypatch):
    red = Red(monkeypatch)
    red.puerta.clear()

    descargas.iniciar(PRUEBA)
    await a_mitad_del_modelo(descargas, PRUEBA)
    with pytest.raises(ErrorDeVoz, match="ya se está descargando"):
        descargas.iniciar(PRUEBA)

    red.puerta.set()
    await terminar(descargas, PRUEBA)
    assert red.llamadas.count(catalogo.url_modelo) == 1


async def test_dos_voces_distintas_pueden_bajar_a_la_vez(catalogo, descargas, monkeypatch):
    red = Red(monkeypatch)
    red.puerta.clear()
    otra = Voz("es_ES-otra-low", len(MODELO), md5(MODELO), md5(CONFIG))
    monkeypatch.setattr(voces, "CATALOGO", {**voces.CATALOGO, otra.id: otra})

    descargas.iniciar(PRUEBA)
    descargas.iniciar(otra.id)

    assert descargas.activa(PRUEBA) and descargas.activa(otra.id)
    red.puerta.set()
    await terminar(descargas, PRUEBA)
    await terminar(descargas, otra.id)
    assert voces.instalada(PRUEBA) and voces.instalada(otra.id)


async def test_cancelar_la_descarga_no_deja_temporales(catalogo, descargas, monkeypatch):
    red = Red(monkeypatch)
    red.puerta.clear()

    descargas.iniciar(PRUEBA)
    await a_mitad_del_modelo(descargas, PRUEBA)
    descargas._tareas[PRUEBA].cancel()
    await terminar(descargas, PRUEBA)

    assert not descargas.activa(PRUEBA)
    assert list(config.CARPETA_DE_VOCES.iterdir()) == []


@pytest.mark.parametrize("voz_id", ["https://evil.example/x.onnx", "../fuera", "es_MX-fantasma-high", voces.ESPEAK])
async def test_solo_se_baja_lo_que_esta_en_el_catalogo(descargas, monkeypatch, voz_id):
    red = Red(monkeypatch)

    with pytest.raises(ErrorDeVoz):
        descargas.iniciar(voz_id)

    assert red.llamadas == [] and not descargas.activa(voz_id)


async def test_no_baja_una_voz_ya_instalada(descargas, monkeypatch):
    red = Red(monkeypatch)
    instalar("es_MX-ald-medium")

    with pytest.raises(ErrorDeVoz, match="ya está instalada"):
        descargas.iniciar("es_MX-ald-medium")
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
    app = web.Application()
    async with TestServer(app) as servidor:
        with pytest.raises(aiohttp.ClientResponseError):
            await voces._bajar(str(servidor.make_url("/no-existe")), tmp_path / "x", None)


# --- elegir -----------------------------------------------------------------

def test_elegir_una_voz_instalada_la_guarda(monkeypatch):
    monkeypatch.setattr(voces, "piper_disponible", lambda: True)
    instalar("es_MX-ald-medium")

    voces.elegir("es_MX-ald-medium")

    assert ajustes.obtener("voz") == "es_MX-ald-medium"


def test_elegir_una_voz_no_instalada_es_error_y_no_cambia_nada():
    with pytest.raises(ErrorDeVoz, match="no está instalada"):
        voces.elegir("es_MX-ald-medium")
    with pytest.raises(ErrorDeVoz):
        voces.elegir("es_MX-fantasma-high")

    assert ajustes.obtener("voz") == voces.ESPEAK


def test_elegir_una_voz_de_piper_sin_piper_es_error(monkeypatch):
    monkeypatch.setattr(voces, "piper_disponible", lambda: False)
    instalar("es_MX-ald-medium")

    with pytest.raises(ErrorDeVoz, match="piper-tts"):
        voces.elegir("es_MX-ald-medium")


def test_se_puede_volver_a_espeak(monkeypatch):
    monkeypatch.setattr(voces, "piper_disponible", lambda: False)
    ajustes.guardar(voz="es_MX-ald-medium")

    voces.elegir(voces.ESPEAK)

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


# --- borrar -----------------------------------------------------------------

def test_borrar_quita_los_dos_archivos():
    instalar("es_MX-ald-medium")

    voces.borrar("es_MX-ald-medium")

    assert not voces.instalada("es_MX-ald-medium")
    assert list(config.CARPETA_DE_VOCES.iterdir()) == []


def test_no_se_borra_la_voz_en_uso():
    instalar("es_MX-ald-medium")
    ajustes.guardar(voz="es_MX-ald-medium")

    with pytest.raises(ErrorDeVoz, match="en uso"):
        voces.borrar("es_MX-ald-medium")

    assert voces.instalada("es_MX-ald-medium")


@pytest.mark.parametrize("voz_id", [voces.ESPEAK, "es_MX-fantasma-high", "../../x"])
def test_no_se_borra_lo_que_no_es_una_voz_de_piper(voz_id):
    with pytest.raises(ErrorDeVoz):
        voces.borrar(voz_id)


async def test_no_se_borra_una_voz_que_se_esta_bajando(catalogo, descargas, monkeypatch):
    red = Red(monkeypatch)
    red.puerta.clear()
    descargas.iniciar(PRUEBA)
    await a_mitad_del_modelo(descargas, PRUEBA)

    with pytest.raises(ErrorDeVoz, match="descargando"):
        voces.borrar(PRUEBA)

    red.puerta.set()
    await terminar(descargas, PRUEBA)


# --- listar -----------------------------------------------------------------

def test_listar_da_el_estado_de_cada_voz(descargas):
    instalar("es_MX-ald-medium")
    instalar("es_ES-davefx-medium")
    ajustes.guardar(voz="es_ES-davefx-medium")
    descargas._error["es_MX-claude-high"] = "sin red"

    lista = {v["id"]: v for v in voces.listar()}

    assert list(lista) == list(CATALOGO)
    assert lista["es_ES-davefx-medium"]["en_uso"] and lista["es_ES-davefx-medium"]["instalada"]
    assert lista["es_MX-ald-medium"]["instalada"] and not lista["es_MX-ald-medium"]["en_uso"]
    assert lista["es_MX-ald-medium"]["pais"] == "México" and lista["es_MX-ald-medium"]["calidad"] == "medium"
    assert lista["es_MX-claude-high"]["error"] == "sin red" and not lista["es_MX-claude-high"]["instalada"]
    assert not lista["es_AR-daniela-high"]["instalada"] and not lista["es_AR-daniela-high"]["descargando"]
    assert lista[voces.ESPEAK]["instalada"] and not lista[voces.ESPEAK]["en_uso"]


async def test_listar_marca_la_que_se_esta_bajando(catalogo, descargas, monkeypatch):
    red = Red(monkeypatch)
    red.puerta.clear()
    descargas.iniciar(PRUEBA)
    await a_mitad_del_modelo(descargas, PRUEBA)

    voz = next(v for v in voces.listar() if v["id"] == PRUEBA)

    assert voz["descargando"] and voz["progreso"] == 50
    red.puerta.set()
    await terminar(descargas, PRUEBA)
