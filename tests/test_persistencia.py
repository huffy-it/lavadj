"""La cola de cada servidor en disco: la foto, el archivo y lo que pasa cuando el archivo no sirve."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import wavelink

from core import config, persistencia
from core.persistencia import VIGENCIA

AHORA = 1_000_000.0


def _raw(titulo):
    return {
        "encoded": f"enc-{titulo}",
        "info": {
            "identifier": titulo, "isSeekable": True, "author": "alguien", "length": 180_000,
            "isStream": False, "position": 0, "title": titulo,
            "uri": f"https://www.youtube.com/watch?v={titulo}", "sourceName": "youtube",
        },
        "pluginInfo": {},
        "userData": {},
    }


class Pista:
    def __init__(self, titulo, clip=False, raw=None):
        self.raw_data = _raw(titulo) if raw is None else raw
        self.clip = clip


class Cola(list):
    mode = wavelink.QueueMode.normal


class Player:
    def __init__(self, actual=None, cola=(), modo=wavelink.QueueMode.normal, canal=7, texto=8):
        self.channel = None if canal is None else type("Canal", (), {"id": canal})()
        self.text_channel = type("Canal", (), {"id": texto})() if texto else None
        self.current = actual
        self.queue = Cola(cola)
        self.queue.mode = modo
        self.volume = 80
        self.paused = False


def _es_clip(pista):
    return pista.clip


def _foto(player, **kw):
    kw.setdefault("posicion", 95_000)
    kw.setdefault("autoplay", True)
    return persistencia.tomar(player, es_clip=_es_clip, ahora=AHORA, **kw)


def _foto_cualquiera(**cambios):
    return {**_foto(Player(Pista("a"), [Pista("b")])), **cambios}


# --- la foto ---------------------------------------------------------------------

def test_la_foto_lleva_todo_lo_que_hace_falta_para_retomar():
    player = Player(Pista("sonando"), [Pista("uno"), Pista("dos")], modo=wavelink.QueueMode.loop)
    player.paused = True

    foto = _foto(player)

    assert foto == {
        "voz": 7, "texto": 8, "actual": _raw("sonando"), "posicion": 95_000,
        "cola": [_raw("uno"), _raw("dos")], "modo": "loop", "volumen": 80,
        "pausado": True, "autoplay": True, "hora": AHORA,
    }


def test_los_cortes_del_dj_no_se_guardan():
    player = Player(Pista("sonando"), [Pista("corte", clip=True), Pista("tema"), Pista("otro corte", clip=True)])

    assert _foto(player)["cola"] == [_raw("tema")]


def test_quien_pidio_el_tema_viaja_en_la_foto():
    pista = Pista("tema")
    pista.requester = "ana"
    assert _foto(Player(None, [pista]))["cola"] == [{**_raw("tema"), "pedido_por": "ana"}]


def test_si_lo_que_suena_es_un_corte_del_dj_no_es_el_tema_en_curso():
    foto = _foto(Player(Pista("corte", clip=True), [Pista("sigue")]))

    assert foto["actual"] is None
    assert foto["posicion"] == 0
    assert foto["cola"] == [_raw("sigue")]


def test_sin_tema_ni_cola_no_hay_nada_que_retomar():
    assert _foto(Player()) is None
    assert _foto(Player(Pista("corte", clip=True), [Pista("otro", clip=True)])) is None


def test_sin_canal_de_voz_no_hay_foto():
    assert _foto(Player(Pista("a"), canal=None)) is None


def test_sin_canal_de_texto_la_foto_lo_deja_vacio():
    assert _foto(Player(Pista("a"), texto=None))["texto"] is None


def test_la_foto_toma_la_hora_del_reloj_si_no_se_le_da_una(monkeypatch):
    monkeypatch.setattr(persistencia, "time", SimpleNamespace(time=lambda: 123.0))

    assert persistencia.tomar(Player(Pista("a")), posicion=0, autoplay=False, es_clip=_es_clip)["hora"] == 123.0


def test_una_pista_que_no_cabe_en_json_se_deja_fuera_y_no_rompe_la_foto(caplog):
    mala = Pista("mala", raw={"encoded": "x", "info": object()})
    player = Player(Pista("sonando"), [mala, Pista("buena")])

    with caplog.at_level("WARNING", logger="persistencia"):
        foto = _foto(player)

    assert foto["cola"] == [_raw("buena")]
    assert "no se puede guardar" in caplog.text
    json.dumps(foto, allow_nan=False)


def test_si_el_tema_en_curso_no_cabe_en_json_queda_sin_tema_pero_con_cola():
    foto = _foto(Player(Pista("mala", raw={"info": object()}), [Pista("buena")]))

    assert foto["actual"] is None
    assert foto["cola"] == [_raw("buena")]


# --- la huella: qué cuenta como "cambió" --------------------------------------------

def test_la_huella_ignora_la_hora_y_la_posicion():
    antes = {1: _foto_cualquiera()}
    despues = {1: _foto_cualquiera(hora=AHORA + 600, posicion=5_000)}

    assert persistencia.huella(antes) == persistencia.huella(despues)


@pytest.mark.parametrize("cambio", [
    {"cola": []}, {"actual": _raw("otro")}, {"pausado": True}, {"volumen": 10},
    {"modo": "loop"}, {"autoplay": False}, {"voz": 99}, {"texto": 99},
])
def test_la_huella_cambia_con_cualquier_otra_cosa(cambio):
    assert persistencia.huella({1: _foto_cualquiera()}) != persistencia.huella({1: _foto_cualquiera(**cambio)})


def test_la_huella_distingue_los_servidores():
    assert persistencia.huella({1: _foto_cualquiera()}) != persistencia.huella({2: _foto_cualquiera()})
    assert persistencia.huella({}) != persistencia.huella({1: _foto_cualquiera()})


# --- el archivo -------------------------------------------------------------------

def test_ida_y_vuelta_por_el_archivo(archivo_de_colas):
    fotos = {5: _foto(Player(Pista("a"), [Pista("b"), Pista("corte", clip=True), Pista("c")])), 6: _foto_cualquiera()}

    persistencia.guardar(fotos)

    assert archivo_de_colas.exists()
    assert persistencia.cargar(ahora=AHORA) == fotos
    assert persistencia.cargar(ahora=AHORA)[5]["cola"] == [_raw("b"), _raw("c")]


def test_el_archivo_es_json_corriente(archivo_de_colas):
    persistencia.guardar({5: _foto_cualquiera()})

    crudo = json.loads(archivo_de_colas.read_text(encoding="utf-8"))

    assert crudo["version"] == persistencia.VERSION
    assert list(crudo["servidores"]) == ["5"]


def test_se_crea_la_carpeta_si_no_existe(tmp_path):
    ruta = tmp_path / "otra" / "mas" / "colas.json"

    persistencia.guardar({1: _foto_cualquiera()}, ruta)

    assert ruta.exists()


def test_sin_ruta_se_usa_la_de_la_configuracion(archivo_de_colas):
    persistencia.guardar({1: _foto_cualquiera()})

    assert archivo_de_colas.exists()
    assert archivo_de_colas != config.PROJECT_ROOT / "estado" / "colas.json"


def test_guardar_reemplaza_todo_lo_anterior(archivo_de_colas):
    persistencia.guardar({1: _foto_cualquiera(), 2: _foto_cualquiera()})
    persistencia.guardar({2: _foto_cualquiera()})

    assert list(persistencia.cargar(ahora=AHORA)) == [2]


def test_el_archivo_de_estado_no_se_versiona():
    assert "estado/" in (config.PROJECT_ROOT / ".gitignore").read_text().split()


# --- escritura atómica ---------------------------------------------------------------

def _viejo(archivo_de_colas):
    persistencia.guardar({1: _foto_cualquiera()})
    return archivo_de_colas.read_bytes()


def test_si_falla_el_cambio_de_archivo_el_anterior_queda_intacto(archivo_de_colas, monkeypatch):
    viejo = _viejo(archivo_de_colas)

    def _falla(origen, destino):
        raise OSError("disco lleno")

    monkeypatch.setattr(persistencia.os, "replace", _falla)
    with pytest.raises(OSError):
        persistencia.guardar({2: _foto_cualquiera()})

    assert archivo_de_colas.read_bytes() == viejo
    assert list(archivo_de_colas.parent.iterdir()) == [archivo_de_colas]


def test_si_falla_la_escritura_a_medias_el_anterior_queda_intacto(archivo_de_colas, monkeypatch):
    viejo = _viejo(archivo_de_colas)

    def _falla(fd):
        raise OSError("se cortó la luz")

    monkeypatch.setattr(persistencia.os, "fsync", _falla)
    with pytest.raises(OSError):
        persistencia.guardar({2: _foto_cualquiera()})

    assert archivo_de_colas.read_bytes() == viejo
    assert list(archivo_de_colas.parent.iterdir()) == [archivo_de_colas]


def test_lo_que_no_es_serializable_no_toca_el_archivo(archivo_de_colas):
    viejo = _viejo(archivo_de_colas)

    with pytest.raises(TypeError):
        persistencia.guardar({2: _foto_cualquiera(actual={"info": object()})})

    assert archivo_de_colas.read_bytes() == viejo
    assert list(archivo_de_colas.parent.iterdir()) == [archivo_de_colas]


def test_el_temporal_se_escribe_junto_al_archivo(archivo_de_colas, monkeypatch):
    visto = []
    cambiar = persistencia.os.replace

    def _espia(origen, destino):
        visto.append((origen, destino))
        cambiar(origen, destino)

    monkeypatch.setattr(persistencia.os, "replace", _espia)
    persistencia.guardar({1: _foto_cualquiera()})

    [(origen, destino)] = visto
    assert destino == archivo_de_colas
    assert origen != destino
    assert Path(origen).parent == destino.parent


# --- archivo ausente, corrupto o raro ----------------------------------------------------

def test_sin_archivo_no_hay_fotos_ni_ruido(caplog):
    with caplog.at_level("DEBUG", logger="persistencia"):
        assert persistencia.cargar() == {}

    assert caplog.records == []


@pytest.mark.parametrize("contenido", [
    "{esto no es json",
    "",
    b"\xff\xfe\x00\x80 bytes que no son UTF-8",
    "[]",
    "null",
    '{"version": 99, "servidores": {}}',
    '{"servidores": {}}',
    '{"version": 1, "servidores": []}',
    '{"version": 1}',
])
def test_un_archivo_que_no_sirve_se_trata_como_vacio_y_queda_en_el_log(archivo_de_colas, caplog, contenido):
    archivo_de_colas.parent.mkdir(parents=True)
    if isinstance(contenido, bytes):
        archivo_de_colas.write_bytes(contenido)
    else:
        archivo_de_colas.write_text(contenido, encoding="utf-8")

    with caplog.at_level("WARNING", logger="persistencia"):
        assert persistencia.cargar(ahora=AHORA) == {}

    assert "lo trato como vacío" in caplog.text


def test_si_el_archivo_es_una_carpeta_tampoco_rompe(archivo_de_colas, caplog):
    archivo_de_colas.mkdir(parents=True)

    with caplog.at_level("WARNING", logger="persistencia"):
        assert persistencia.cargar(ahora=AHORA) == {}

    assert "lo trato como vacío" in caplog.text


def _con_entradas(archivo_de_colas, servidores):
    archivo_de_colas.parent.mkdir(parents=True)
    archivo_de_colas.write_text(json.dumps({"version": persistencia.VERSION, "servidores": servidores}))


@pytest.mark.parametrize("rara", [
    "no es un diccionario",
    {"voz": 1},
    {**_foto_cualquiera(), "extra": 1},
    {**_foto_cualquiera(), "voz": "siete"},
    {**_foto_cualquiera(), "cola": "no es lista"},
    {**_foto_cualquiera(), "cola": ["no es una pista"]},
    {**_foto_cualquiera(), "actual": "no es un dict"},
    {**_foto_cualquiera(), "hora": "ayer"},
])
def test_una_foto_rara_se_descarta_y_las_buenas_se_quedan(archivo_de_colas, caplog, rara):
    _con_entradas(archivo_de_colas, {"1": rara, "2": _foto_cualquiera()})

    with caplog.at_level("WARNING", logger="persistencia"):
        fotos = persistencia.cargar(ahora=AHORA)

    assert list(fotos) == [2]
    assert "Descarto la foto de '1'" in caplog.text


def test_un_id_de_servidor_que_no_es_numero_se_descarta(archivo_de_colas):
    _con_entradas(archivo_de_colas, {"abc": _foto_cualquiera(), "3": _foto_cualquiera()})

    assert list(persistencia.cargar(ahora=AHORA)) == [3]


# --- vigencia --------------------------------------------------------------------------

def test_una_foto_vieja_no_se_retoma(archivo_de_colas, caplog):
    persistencia.guardar({
        1: _foto_cualquiera(hora=AHORA - VIGENCIA - 1),
        2: _foto_cualquiera(hora=AHORA - VIGENCIA + 1),
        3: _foto_cualquiera(hora=AHORA),
    })

    with caplog.at_level("INFO", logger="persistencia"):
        fotos = persistencia.cargar(ahora=AHORA)

    assert sorted(fotos) == [2, 3]
    assert "no la retomo" in caplog.text


def test_la_vigencia_se_mide_contra_el_reloj_si_no_se_da_la_hora(monkeypatch):
    persistencia.guardar({1: _foto_cualquiera(hora=AHORA)})
    monkeypatch.setattr(persistencia, "time", SimpleNamespace(time=lambda: AHORA + VIGENCIA + 60))

    assert persistencia.cargar() == {}


# --- borrar un servidor -------------------------------------------------------------------

def test_borrar_saca_solo_ese_servidor(archivo_de_colas):
    persistencia.guardar({1: _foto_cualquiera(), 2: _foto_cualquiera()})

    persistencia.borrar(1)

    assert list(persistencia.cargar(ahora=AHORA)) == [2]


def test_borrar_sin_archivo_no_crea_nada(archivo_de_colas):
    persistencia.borrar(1)

    assert not archivo_de_colas.parent.exists()


def test_borrar_un_servidor_que_no_esta_no_reescribe_el_archivo(archivo_de_colas, monkeypatch):
    viejo = _viejo(archivo_de_colas)
    monkeypatch.setattr(persistencia, "_escribir", lambda *a: pytest.fail("no había nada que borrar"))

    persistencia.borrar(99)

    assert archivo_de_colas.read_bytes() == viejo


def test_borrar_con_el_archivo_corrupto_no_rompe(archivo_de_colas):
    archivo_de_colas.parent.mkdir(parents=True)
    archivo_de_colas.write_text("{roto")

    persistencia.borrar(1)

    assert archivo_de_colas.read_text() == "{roto"


def test_si_no_se_puede_escribir_borrar_lo_dice_y_sigue(archivo_de_colas, monkeypatch, caplog):
    persistencia.guardar({1: _foto_cualquiera()})

    def _falla(origen, destino):
        raise OSError("sin permiso")

    monkeypatch.setattr(persistencia.os, "replace", _falla)
    with caplog.at_level(logging.WARNING, logger="persistencia"):
        persistencia.borrar(1)

    assert "No pude borrar la foto de 1" in caplog.text
