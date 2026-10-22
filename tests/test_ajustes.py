"""Los ajustes persistentes: valores por defecto, escritura atómica y tolerancia a archivos rotos."""

import json
import os

import pytest

from core import ajustes, config, guionista


def test_sin_archivo_valen_los_valores_por_defecto():
    assert ajustes.cargar() == {"voz": "espeak", "filtro_radio": True, "guionista": None}
    assert ajustes.obtener("voz") == "espeak"


def test_lo_guardado_se_lee_de_vuelta_y_queda_en_el_archivo():
    ajustes.guardar(voz="es_MX-ald-medium")
    ajustes.guardar(filtro_radio=False)

    assert ajustes.cargar() == {"voz": "es_MX-ald-medium", "filtro_radio": False, "guionista": None}
    assert json.loads(config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8"))["voz"] == "es_MX-ald-medium"


def test_los_tests_no_escriben_en_el_estado_del_proyecto():
    assert config.PROJECT_ROOT / "estado" not in config.ARCHIVO_DE_AJUSTES.parents


def test_guardar_crea_la_carpeta():
    assert not config.ARCHIVO_DE_AJUSTES.parent.exists()
    ajustes.guardar(filtro_radio=False)
    assert config.ARCHIVO_DE_AJUSTES.is_file()


@pytest.mark.parametrize("contenido", ["{no es json", "[1, 2]", "", "null", b"\xff\xfe\x00"])
def test_un_archivo_roto_o_raro_cae_a_los_valores_por_defecto(contenido):
    config.ARCHIVO_DE_AJUSTES.parent.mkdir(parents=True)
    config.ARCHIVO_DE_AJUSTES.write_bytes(contenido if isinstance(contenido, bytes) else contenido.encode())

    assert ajustes.cargar() == ajustes.POR_DEFECTO


def test_un_valor_del_tipo_equivocado_se_ignora_sin_tirar_los_demas():
    config.ARCHIVO_DE_AJUSTES.parent.mkdir(parents=True)
    config.ARCHIVO_DE_AJUSTES.write_text(json.dumps({"voz": 7, "filtro_radio": False}), encoding="utf-8")

    assert ajustes.cargar() == {"voz": "espeak", "filtro_radio": False, "guionista": None}


def test_guardar_un_tipo_equivocado_es_error_y_no_toca_el_archivo():
    ajustes.guardar(voz="es_MX-ald-medium")
    antes = config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        ajustes.guardar(filtro_radio="si")

    assert config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8") == antes


def test_guardar_respeta_las_claves_que_este_modulo_no_conoce():
    config.ARCHIVO_DE_AJUSTES.parent.mkdir(parents=True)
    config.ARCHIVO_DE_AJUSTES.write_text(json.dumps({"futura": "x"}), encoding="utf-8")

    ajustes.guardar(voz="es_MX-ald-medium")

    assert json.loads(config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8")) == {
        "futura": "x", "voz": "es_MX-ald-medium",
    }
    assert ajustes.obtener("futura") == "x"


# --- guionista ---------------------------------------------------------------

def test_sin_guionista_guardado_el_valor_es_none():
    assert ajustes.obtener("guionista") is None


@pytest.mark.parametrize("nombre", guionista.NOMBRES)
def test_cada_guionista_conocido_se_guarda_y_se_lee_de_vuelta(nombre):
    ajustes.guardar(guionista=nombre)

    assert ajustes.obtener("guionista") == nombre
    assert json.loads(config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8"))["guionista"] == nombre


def test_guardar_none_borra_la_eleccion():
    ajustes.guardar(guionista="claude")
    ajustes.guardar(guionista=None)

    assert ajustes.obtener("guionista") is None


@pytest.mark.parametrize("valor", ["gpt", "", "Claude", 7, ["claude"], True])
def test_guardar_un_guionista_desconocido_es_error_y_no_toca_el_archivo(valor):
    ajustes.guardar(guionista="ollama")
    antes = config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        ajustes.guardar(guionista=valor)

    assert config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8") == antes


@pytest.mark.parametrize("valor", ["gpt", "", 7, ["claude"], False])
def test_un_guionista_raro_en_el_archivo_se_ignora_sin_tirar_los_demas(valor):
    config.ARCHIVO_DE_AJUSTES.parent.mkdir(parents=True)
    config.ARCHIVO_DE_AJUSTES.write_text(json.dumps({"guionista": valor, "voz": "es_MX-ald-medium"}), encoding="utf-8")

    assert ajustes.cargar() == {"voz": "es_MX-ald-medium", "filtro_radio": True, "guionista": None}


def test_si_la_escritura_falla_el_archivo_queda_intacto_y_sin_temporales(monkeypatch):
    ajustes.guardar(voz="es_MX-ald-medium")
    antes = config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8")

    def falla(*_):
        raise OSError("disco lleno")

    monkeypatch.setattr(os, "replace", falla)
    with pytest.raises(OSError):
        ajustes.guardar(voz="es_ES-davefx-medium")

    assert config.ARCHIVO_DE_AJUSTES.read_text(encoding="utf-8") == antes
    assert [p.name for p in config.ARCHIVO_DE_AJUSTES.parent.iterdir()] == ["ajustes.json"]
