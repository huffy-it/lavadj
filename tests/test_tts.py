"""La voz del DJ: cadena de filtros de radio y volumen.

Nadie ejecuta espeak ni ffmpeg de verdad aquí: se intercepta
`asyncio.create_subprocess_exec` y se revisa con qué argumentos se los llamó.
"""

import asyncio

import pytest

from core import tts


class FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return b"", b""

    def kill(self):
        self.killed = True


class Llamadas(list):
    """Las invocaciones registradas, más un dial para romper un binario."""

    def __init__(self):
        super().__init__()
        self.fallar: dict[str, int] = {}


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
        # espeak escribe en lo que sigue a -w; ffmpeg, en el último argumento.
        salida = args[args.index("-w") + 1] if "-w" in args else args[-1]
        codigo = fallas.get(args[0], 0)
        if codigo == 0:
            with open(salida, "wb") as fh:
                fh.write(b"RIFF....WAVE")
        return FakeProc(codigo)

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


def test_purge_cache_barre_los_intermedios(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    (tmp_path / "dj_abc.wav").write_bytes(b"x")
    (tmp_path / "dj_abc.crudo.wav").write_bytes(b"x")
    (tmp_path / "otra_cosa.txt").write_bytes(b"x")

    assert tts.purge_cache() == 2
    assert (tmp_path / "otra_cosa.txt").exists()
