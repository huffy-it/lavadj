"""La voz del DJ: Piper o espeak, filtros de radio y volumen. Se intercepta `create_subprocess_exec`: nada se ejecuta de verdad."""

import asyncio
import sys

import pytest

from core import ajustes, config, tts, voces

PIPER = "es_MX-ald-medium"
ESPEAK, FFMPEG = "/usr/bin/espeak-ng", "/usr/bin/ffmpeg"


class FakeProc:
    def __init__(self, returncode, entradas):
        self.returncode, self.entradas = returncode, entradas

    async def communicate(self, entrada=None):
        self.entradas.append(entrada)
        return b"", b""

    def kill(self):
        pass


class Llamadas(list):
    """Las invocaciones (listas de argumentos), con diales para romper un binario."""

    def __init__(self):
        super().__init__()
        self.fallar: dict[str, int] = {}
        self.entradas: list = []          # lo que cada proceso recibió por stdin
        self.sin_audio: set[str] = set()  # binarios que terminan bien pero no escriben nada


@pytest.fixture
def exec_falso(monkeypatch, tmp_path):
    """Reemplaza los subprocesos y deja escrito el wav que cada uno prometía."""
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(tts, "ESPEAK_BIN", ESPEAK)
    monkeypatch.setattr(tts, "FFMPEG_BIN", FFMPEG)
    llamadas = Llamadas()

    async def falso_exec(*args, **kwargs):
        llamadas.append(list(args))
        salida = args[-1]     # espeak escribe tras -w; Piper, tras -f; ffmpeg, en el último argumento
        for opcion in ("-w", "-f"):
            if opcion in args:
                salida = args[args.index(opcion) + 1]
        codigo = llamadas.fallar.get(args[0], 0)
        if codigo == 0:
            with open(salida, "wb") as fh:
                fh.write(b"" if args[0] in llamadas.sin_audio else b"RIFF....WAVE")
        return FakeProc(codigo, llamadas.entradas)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", falso_exec)
    return llamadas


async def _decir(texto="hola", volume=100, **kwargs):
    return await tts.synthesize(texto, volume=volume, **kwargs)


def test_la_cadena_con_radio_filtra_y_escala_la_ganancia():
    cadena = tts.filter_chain(100)
    assert all(f in cadena for f in ("highpass=f=400", "lowpass=f=3400", "acompressor"))
    assert f"volume={tts.BASE_GAIN:.2f}" in cadena     # 100% es el volumen histórico, no ganancia 1.0
    assert "volume=0.80" in tts.filter_chain(50) and "volume=3.20" in tts.filter_chain(200)


@pytest.mark.parametrize("radio", [True, False])
def test_el_volumen_queda_topeado(radio):
    assert tts.filter_chain(500, radio) == tts.filter_chain(200, radio)
    assert tts.filter_chain(-20, radio) == tts.filter_chain(0, radio)


def test_sin_filtro_de_radio_solo_queda_el_volumen_con_su_limitador():
    cadena = tts.filter_chain(100, radio=False)
    assert "highpass" not in cadena and "acompressor" not in cadena
    assert "volume=1.00" in cadena and "alimiter" in cadena
    assert "volume=0.50" in tts.filter_chain(50, radio=False)


@pytest.mark.parametrize("volumen", [100, 40])
async def test_sintetiza_pasando_por_espeak_y_despues_ffmpeg_con_el_volumen_pedido(exec_falso, volumen):
    salida = await _decir("hola gente", volumen)

    assert salida is not None and salida.is_file()
    assert len(exec_falso) == 2
    espeak, ffmpeg = exec_falso
    assert espeak[0] == ESPEAK and ffmpeg[0] == FFMPEG
    assert tts.filter_chain(volumen) in ffmpeg
    assert str(salida) == ffmpeg[-1]


async def test_el_texto_va_como_argumento_y_no_por_shell(exec_falso):
    await _decir("; rm -rf ~")
    assert exec_falso[0][-2:] == ["--", "; rm -rf ~"]


async def test_sin_ffmpeg_igual_habla(monkeypatch, exec_falso):
    monkeypatch.setattr(tts, "FFMPEG_BIN", None)
    salida = await _decir()
    assert salida is not None and salida.is_file()
    assert len(exec_falso) == 1          # solo espeak


async def test_si_ffmpeg_falla_se_usa_el_wav_crudo(exec_falso):
    exec_falso.fallar[FFMPEG] = 1
    salida = await _decir()
    assert salida is not None and salida.is_file()
    assert len(exec_falso) == 2


async def test_si_espeak_falla_no_hay_clip(exec_falso):
    exec_falso.fallar[ESPEAK] = 1
    assert await _decir() is None
    assert len(exec_falso) == 1          # ni se intenta filtrar


@pytest.mark.parametrize("filtro", [True, False])
async def test_el_ajuste_del_filtro_decide_la_cadena_de_ffmpeg(exec_falso, filtro):
    ajustes.guardar(filtro_radio=filtro)
    await _decir(volume=70)
    assert tts.filter_chain(70, radio=filtro) in exec_falso[1]
    assert ("highpass=f=400" in " ".join(exec_falso[1])) is filtro


@pytest.fixture
def piper(monkeypatch, exec_falso):
    """Piper disponible, con una voz instalada y elegida."""
    monkeypatch.setattr(voces, "piper_disponible", lambda: True)
    config.CARPETA_DE_VOCES.mkdir(parents=True)
    voces.ruta_modelo(PIPER).write_bytes(b"onnx")
    voces.ruta_config(PIPER).write_bytes(b"{}")
    ajustes.guardar(voz=PIPER)
    return exec_falso


async def test_con_voz_de_piper_habla_piper_y_despues_ffmpeg(piper):
    salida = await _decir("hola gente")

    assert salida is not None and salida.is_file()
    assert len(piper) == 2                       # nada de espeak
    llamada, ffmpeg = piper
    assert llamada[:5] == [sys.executable, "-X", "utf8", "-m", "piper"]
    assert llamada[llamada.index("-m", 5) + 1] == str(voces.ruta_modelo(PIPER))
    assert llamada[llamada.index("-c") + 1] == str(voces.ruta_config(PIPER))
    assert "--length-scale" in llamada
    assert ffmpeg[0] == FFMPEG and tts.filter_chain(100) in ffmpeg


async def test_piper_recibe_el_texto_por_stdin_y_no_como_argumento(piper):
    await _decir("--length-scale 9 ñandú")
    assert piper.entradas[0] == "--length-scale 9 ñandú".encode("utf-8")
    assert "ñandú" not in " ".join(piper[0])


@pytest.mark.parametrize("kwargs", [{"voice": tts.VOICE_EN}, {"voz": voces.ESPEAK}], ids=["ingles", "voz_forzada"])
async def test_las_frases_en_ingles_y_la_voz_forzada_las_lee_espeak_aunque_haya_voz_de_piper(piper, kwargs):
    await _decir("what's up", **kwargs)
    assert piper[0][0] == ESPEAK
    assert piper[0][piper[0].index("-v") + 1] == kwargs.get("voice", tts.VOICE)


@pytest.mark.parametrize("modo", ["falla", "sin_audio"])
async def test_si_piper_falla_o_no_deja_audio_cae_a_espeak(piper, caplog, modo):
    if modo == "falla":
        piper.fallar[sys.executable] = 1
    else:
        piper.sin_audio.add(sys.executable)

    salida = await _decir()

    assert salida is not None and salida.is_file()
    assert [llamada[0] for llamada in piper] == [sys.executable, ESPEAK, FFMPEG]
    assert "sigo con espeak" in caplog.text


@pytest.mark.parametrize("quitar", ["piper", "voz"])
async def test_si_piper_o_la_voz_elegida_no_estan_habla_espeak_y_avisa(piper, monkeypatch, caplog, quitar):
    if quitar == "piper":
        monkeypatch.setattr(voces, "piper_disponible", lambda: False)
    else:
        voces.ruta_modelo(PIPER).unlink()

    assert await _decir() is not None
    assert piper[0][0] == ESPEAK
    assert "uso espeak" in caplog.text


async def test_si_piper_y_espeak_fallan_no_hay_clip(piper):
    piper.fallar[sys.executable] = 1
    piper.fallar[ESPEAK] = 1
    assert await _decir() is None


async def test_sin_espeak_y_con_la_voz_de_siempre_habla_la_primera_de_piper(piper, monkeypatch):
    monkeypatch.setattr(tts, "ESPEAK_BIN", None)
    ajustes.guardar(voz=voces.ESPEAK)

    assert await _decir() is not None
    assert piper[0][0] == sys.executable


def test_available_con_espeak_o_con_piper_y_una_voz(monkeypatch):
    monkeypatch.setattr(tts, "ESPEAK_BIN", ESPEAK)
    assert tts.available()

    monkeypatch.setattr(tts, "ESPEAK_BIN", None)
    monkeypatch.setattr(voces, "piper_disponible", lambda: True)
    assert not tts.available()                       # Piper, pero ninguna voz

    config.CARPETA_DE_VOCES.mkdir(parents=True)
    voces.ruta_modelo(PIPER).write_bytes(b"onnx")
    voces.ruta_config(PIPER).write_bytes(b"{}")
    assert tts.available()

    monkeypatch.setattr(voces, "piper_disponible", lambda: False)
    assert not tts.available()                       # la voz sin Piper no suena


def test_purge_cache_barre_los_intermedios(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    for nombre in ("dj_abc.wav", "dj_abc.crudo.wav", "otra_cosa.txt"):
        (tmp_path / nombre).write_bytes(b"x")

    assert tts.purge_cache() == 2
    assert (tmp_path / "otra_cosa.txt").exists()
