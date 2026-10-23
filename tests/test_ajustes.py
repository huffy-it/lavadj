"""Los ajustes persistentes: valores por defecto, escritura atómica y tolerancia a archivos rotos."""

import json
import os

import pytest

from core import ajustes, config, guionista

VOZ = "es_MX-ald-medium"


def _escribir(contenido):
    config.ARCHIVO_DE_AJUSTES.parent.mkdir(parents=True)
    if not isinstance(contenido, (str, bytes)):
        contenido = json.dumps(contenido)
    config.ARCHIVO_DE_AJUSTES.write_bytes(contenido if isinstance(contenido, bytes) else contenido.encode())


def _texto():
    return config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8")


def test_sin_archivo_valen_los_valores_por_defecto():
    assert ajustes.cargar() == {"voz": "espeak", "filtro_radio": True, "guionista": None}
    assert ajustes.obtener("voz") == "espeak" and ajustes.obtener("guionista") is None


def test_lo_guardado_se_lee_de_vuelta_y_queda_en_el_archivo_creando_la_carpeta():
    assert not config.ARCHIVO_DE_AJUSTES.parent.exists()
    ajustes.guardar(voz=VOZ)
    ajustes.guardar(filtro_radio=False)

    assert ajustes.cargar() == {"voz": VOZ, "filtro_radio": False, "guionista": None}
    assert json.loads(_texto())["voz"] == VOZ


def test_los_tests_no_escriben_en_el_estado_del_proyecto():
    assert config.PROJECT_ROOT / "estado" not in config.ARCHIVO_DE_AJUSTES.parents


@pytest.mark.parametrize("contenido", ["{no es json", "[1, 2]", "", "null", b"\xff\xfe\x00"])
def test_un_archivo_roto_o_raro_cae_a_los_valores_por_defecto(contenido):
    _escribir(contenido)
    assert ajustes.cargar() == ajustes.POR_DEFECTO


@pytest.mark.parametrize("archivo, esperado", [
    ({"voz": 7, "filtro_radio": False}, {"voz": "espeak", "filtro_radio": False, "guionista": None}),
    *[({"guionista": v, "voz": VOZ}, {"voz": VOZ, "filtro_radio": True, "guionista": None})
      for v in ("gpt", "", 7, ["claude"], False)],
])
def test_un_valor_invalido_en_el_archivo_se_ignora_sin_tirar_los_demas(archivo, esperado):
    _escribir(archivo)
    assert ajustes.cargar() == esperado


@pytest.mark.parametrize("cambio", [{"filtro_radio": "si"}, *[{"guionista": v} for v in ("gpt", "", "Claude", 7, ["claude"], True)]])
def test_guardar_un_valor_invalido_es_error_y_no_toca_el_archivo(cambio):
    ajustes.guardar(voz=VOZ)
    antes = _texto()
    with pytest.raises(ValueError):
        ajustes.guardar(**cambio)
    assert _texto() == antes


def test_guardar_respeta_las_claves_que_este_modulo_no_conoce():
    _escribir({"futura": "x"})
    ajustes.guardar(voz=VOZ)
    assert json.loads(_texto()) == {"futura": "x", "voz": VOZ}
    assert ajustes.obtener("futura") == "x"


@pytest.mark.parametrize("nombre", guionista.NOMBRES)
def test_cada_guionista_conocido_se_guarda_se_lee_y_none_borra_la_eleccion(nombre):
    ajustes.guardar(guionista=nombre)
    assert ajustes.obtener("guionista") == nombre
    assert json.loads(_texto())["guionista"] == nombre
    ajustes.guardar(guionista=None)
    assert ajustes.obtener("guionista") is None


def test_si_la_escritura_falla_el_archivo_queda_intacto_y_sin_temporales(monkeypatch):
    ajustes.guardar(voz=VOZ)
    antes = _texto()

    def falla(*_):
        raise OSError("disco lleno")

    monkeypatch.setattr(os, "replace", falla)
    with pytest.raises(OSError):
        ajustes.guardar(voz="es_ES-davefx-medium")

    assert _texto() == antes
    assert [p.name for p in config.ARCHIVO_DE_AJUSTES.parent.iterdir()] == ["ajustes.json"]
