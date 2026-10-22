"""La voz del DJ: Piper o espeak, cadena de filtros de radio y volumen.

Nadie ejecuta espeak, Piper ni ffmpeg de verdad aquí: se intercepta
`asyncio.create_subprocess_exec` y se revisa con qué argumentos se los llamó.
"""

import asyncio
import sys

import pytest

from core import ajustes, config, tts, voces

PIPER = "es_MX-ald-medium"


class FakeProc:
    def __init__(self, returncode=0, entradas=None):
        self.returncode = returncode
        self.entradas = entradas if entradas is not None else []
        self.killed = False

    async def communicate(self, entrada=None):
        self.entradas.append(entrada)
        return b"", b""

    def kill(self):
        self.killed = True


class Llamadas(list):
    """Las invocaciones registradas, más un dial para romper un binario."""

    def __init__(self):
        super().__init__()
        self.fallar: dict[str, int] = {}
        self.entradas: list = []          # lo que cada proceso recibió por stdin
        self.sin_audio: set[str] = set()  # binarios que terminan bien pero no escriben nada


@pytest.fixture
def exec_falso(monkeypatch, tmp_path):
    """Reemplaza los subprocesos y deja escrito el wav que cada uno prometía.

    Devuelve la lista de invocaciones (cada una, la lista de argumentos).
    """
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(tts, "ESPEAK_BIN", "/usr/bin/espeak-ng")
    monkeypatch.setattr(tts, "FFMPEG_BIN", "/usr/bin/ffmpeg")

    llamadas = Llamadas()
    fallas = llamadas.fallar

    async def falso_exec(*args, **kwargs):
        llamadas.append(list(args))
        # espeak escribe en lo que sigue a -w; Piper, a -f; ffmpeg, en el último argumento.
        salida = args[-1]
        for opcion in ("-w", "-f"):
            if opcion in args:
                salida = args[args.index(opcion) + 1]
        codigo = fallas.get(args[0], 0)
        if codigo == 0:
            with open(salida, "wb") as fh:
                fh.write(b"" if args[0] in llamadas.sin_audio else b"RIFF....WAVE")
        return FakeProc(codigo, llamadas.entradas)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", falso_exec)
    return llamadas


# --- cadena de filtros ------------------------------------------------------

def test_la_cadena_trae_el_filtro_de_radio():
    cadena = tts.filter_chain(100)
    assert "highpass=f=400" in cadena
    assert "lowpass=f=3400" in cadena
    assert "acompressor" in cadena


def test_al_100_suena_como_antes():
    """100% es el volumen histórico del DJ, no ganancia 1.0."""
    assert f"volume={tts.BASE_GAIN:.2f}" in tts.filter_chain(100)


def test_el_volumen_escala_la_ganancia():
    assert "volume=0.80" in tts.filter_chain(50)
    assert "volume=3.20" in tts.filter_chain(200)


def test_el_volumen_queda_topeado():
    assert tts.filter_chain(500) == tts.filter_chain(200)
    assert tts.filter_chain(-20) == tts.filter_chain(0)


def test_sin_filtro_de_radio_solo_queda_el_volumen_con_su_limitador():
    cadena = tts.filter_chain(100, radio=False)
    assert "highpass" not in cadena and "acompressor" not in cadena
    assert "volume=1.00" in cadena and "alimiter" in cadena
    assert "volume=0.50" in tts.filter_chain(50, radio=False)
    assert tts.filter_chain(900, radio=False) == tts.filter_chain(200, radio=False)


# --- síntesis ---------------------------------------------------------------

def test_sintetiza_pasando_por_espeak_y_despues_ffmpeg(exec_falso):
    salida = asyncio.run(tts.synthesize("hola gente", volume=100))

    assert salida is not None and salida.is_file()
    assert len(exec_falso) == 2
    espeak, ffmpeg = exec_falso
    assert espeak[0] == "/usr/bin/espeak-ng"
    assert ffmpeg[0] == "/usr/bin/ffmpeg"
    assert tts.filter_chain(100) in ffmpeg
    assert str(salida) == ffmpeg[-1]


def test_el_volumen_pedido_llega_a_ffmpeg(exec_falso):
    asyncio.run(tts.synthesize("hola", volume=40))
    assert tts.filter_chain(40) in exec_falso[1]


def test_el_texto_va_como_argumento_y_no_por_shell(exec_falso):
    asyncio.run(tts.synthesize("; rm -rf ~", volume=100))
    espeak = exec_falso[0]
    assert espeak[-1] == "; rm -rf ~"
    assert espeak[-2] == "--"


def test_sin_ffmpeg_igual_habla(monkeypatch, exec_falso):
    """El DJ nunca es fatal: sin ffmpeg se pierde el filtro, no la voz."""
    monkeypatch.setattr(tts, "FFMPEG_BIN", None)

    salida = asyncio.run(tts.synthesize("hola", volume=100))

    assert salida is not None and salida.is_file()
    assert len(exec_falso) == 1          # solo espeak


def test_si_ffmpeg_falla_se_usa_el_wav_crudo(exec_falso):
    exec_falso.fallar["/usr/bin/ffmpeg"] = 1

    salida = asyncio.run(tts.synthesize("hola", volume=100))

    assert salida is not None and salida.is_file()
    assert len(exec_falso) == 2


def test_si_espeak_falla_no_hay_clip(exec_falso):
    exec_falso.fallar["/usr/bin/espeak-ng"] = 1

    assert asyncio.run(tts.synthesize("hola", volume=100)) is None
    assert len(exec_falso) == 1          # ni se intenta filtrar


# --- filtro de radio (ajuste) -----------------------------------------------

@pytest.mark.parametrize("filtro", [True, False])
def test_el_ajuste_del_filtro_decide_la_cadena_de_ffmpeg(exec_falso, filtro):
    ajustes.guardar(filtro_radio=filtro)

    asyncio.run(tts.synthesize("hola", volume=70))

    assert tts.filter_chain(70, radio=filtro) in exec_falso[1]
    assert ("highpass=f=400" in " ".join(exec_falso[1])) is filtro


def test_por_defecto_el_filtro_de_radio_esta_encendido(exec_falso):
    asyncio.run(tts.synthesize("hola", volume=100))
    assert tts.filter_chain(100) in exec_falso[1]


# --- Piper ------------------------------------------------------------------

@pytest.fixture
def piper(monkeypatch, exec_falso):
    """Piper disponible, con una voz instalada y elegida."""
    monkeypatch.setattr(voces, "piper_disponible", lambda: True)
    config.CARPETA_DE_VOCES.mkdir(parents=True)
    voces.ruta_modelo(PIPER).write_bytes(b"onnx")
    voces.ruta_config(PIPER).write_bytes(b"{}")
    ajustes.guardar(voz=PIPER)
    return exec_falso


def test_con_voz_de_piper_habla_piper_y_despues_ffmpeg(piper):
    salida = asyncio.run(tts.synthesize("hola gente", volume=100))

    assert salida is not None and salida.is_file()
    assert len(piper) == 2                       # nada de espeak
    llamada, ffmpeg = piper
    assert llamada[:5] == [sys.executable, "-X", "utf8", "-m", "piper"]
    assert llamada[llamada.index("-m", 5) + 1] == str(voces.ruta_modelo(PIPER))
    assert llamada[llamada.index("-c") + 1] == str(voces.ruta_config(PIPER))
    assert "--length-scale" in llamada
    assert ffmpeg[0] == "/usr/bin/ffmpeg" and tts.filter_chain(100) in ffmpeg


def test_piper_recibe_el_texto_por_stdin_y_no_como_argumento(piper):
    asyncio.run(tts.synthesize("--length-scale 9 ñandú", volume=100))

    assert piper.entradas[0] == "--length-scale 9 ñandú".encode("utf-8")
    assert "ñandú" not in " ".join(piper[0])


def test_las_frases_en_ingles_las_lee_espeak_aunque_haya_voz_de_piper(piper):
    asyncio.run(tts.synthesize("what's up", voice=tts.VOICE_EN, volume=100))

    assert piper[0][0] == "/usr/bin/espeak-ng"
    assert piper[0][piper[0].index("-v") + 1] == tts.VOICE_EN


def test_la_voz_forzada_manda_sobre_el_ajuste(piper):
    asyncio.run(tts.synthesize("hola", volume=100, voz=voces.ESPEAK))

    assert piper[0][0] == "/usr/bin/espeak-ng"


def test_si_piper_falla_cae_a_espeak(piper, caplog):
    piper.fallar[sys.executable] = 1

    salida = asyncio.run(tts.synthesize("hola", volume=100))

    assert salida is not None and salida.is_file()
    assert [llamada[0] for llamada in piper] == [sys.executable, "/usr/bin/espeak-ng", "/usr/bin/ffmpeg"]
    assert "sigo con espeak" in caplog.text


def test_si_piper_no_deja_audio_cae_a_espeak(piper):
    piper.sin_audio.add(sys.executable)

    salida = asyncio.run(tts.synthesize("hola", volume=100))

    assert salida is not None and salida.is_file()
    assert piper[1][0] == "/usr/bin/espeak-ng"


def test_sin_piper_instalado_habla_espeak_y_avisa(piper, monkeypatch, caplog):
    monkeypatch.setattr(voces, "piper_disponible", lambda: False)

    salida = asyncio.run(tts.synthesize("hola", volume=100))

    assert salida is not None
    assert piper[0][0] == "/usr/bin/espeak-ng"
    assert "uso espeak" in caplog.text


def test_voz_elegida_pero_borrada_cae_a_espeak(piper):
    voces.ruta_modelo(PIPER).unlink()

    assert asyncio.run(tts.synthesize("hola", volume=100)) is not None
    assert piper[0][0] == "/usr/bin/espeak-ng"


def test_si_piper_y_espeak_fallan_no_hay_clip(piper):
    piper.fallar[sys.executable] = 1
    piper.fallar["/usr/bin/espeak-ng"] = 1

    assert asyncio.run(tts.synthesize("hola", volume=100)) is None


def test_sin_espeak_y_con_la_voz_de_siempre_habla_la_primera_de_piper(piper, monkeypatch):
    monkeypatch.setattr(tts, "ESPEAK_BIN", None)
    ajustes.guardar(voz=voces.ESPEAK)

    assert asyncio.run(tts.synthesize("hola", volume=100)) is not None
    assert piper[0][0] == sys.executable


def test_available_con_espeak(monkeypatch):
    monkeypatch.setattr(tts, "ESPEAK_BIN", "/usr/bin/espeak-ng")
    assert tts.available()


def test_available_sin_espeak_pide_piper_con_una_voz(monkeypatch):
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
    (tmp_path / "dj_abc.wav").write_bytes(b"x")
    (tmp_path / "dj_abc.crudo.wav").write_bytes(b"x")
    (tmp_path / "otra_cosa.txt").write_bytes(b"x")

    assert tts.purge_cache() == 2
    assert (tmp_path / "otra_cosa.txt").exists()
