import asyncio
import random

import pytest

from core import dj as dj_mod
from core.dj import DJ, Clip, sanitize


class FakeTrack:
    def __init__(self, title="Daft Punk - Around the World", source="https://x/1"):
        self.title = title
        self.source = source


@pytest.fixture
def dj(tmp_path, monkeypatch):
    instance = DJ()
    # No queremos que los tests dependan de que exista el JSON real.
    instance.phrases = {
        "bumper": ["Estás escuchando LavaDJ FM."],
        "ad": ["Auspicia la deuda."],
        "outro": ["Eso fue lo que fue."],
        "intro": ["Ahora viene {titulo}, de {artista}."],
        "noticia": ["Boletín. {titular}. Sin comentarios."],
        "slang": ["no aura"],
    }
    instance.enabled = True
    return instance


# --- saneado ----------------------------------------------------------------

def test_sanitize_saca_urls_y_emoji():
    out = sanitize("Mira esto https://ejemplo.com/x 🎵 ahora")
    assert "http" not in out
    assert "🎵" not in out
    assert out == "Mira esto ahora"


def test_sanitize_colapsa_saltos_de_linea():
    assert sanitize("una\n\nlínea\tsola") == "una línea sola"


def test_sanitize_respeta_el_tope():
    out = sanitize("palabra " * 100)
    assert len(out) <= dj_mod.MAX_CHARS


def test_sanitize_corta_en_frase_completa():
    # Con un punto pasada la mitad del tope, debe cortar ahí y no a mitad de palabra.
    texto = "x" * 150 + ". " + "y " * 100
    out = sanitize(texto)
    assert out == "x" * 150 + "."


def test_sanitize_corte_duro_respeta_el_tope():
    # Sin ningún punto donde cortar, el corte duro igual tiene que caber.
    out = sanitize("y " * 200)
    assert out.endswith(".")
    assert len(out) <= dj_mod.MAX_CHARS


# --- cuándo habla -----------------------------------------------------------

def test_no_habla_dos_veces_seguidas(dj):
    dj._spoke_last[1] = True
    # Aunque el dado diga que sí, no debe hablar.
    random.seed(0)
    assert dj.should_speak(1) is False


def test_forzado_habla_aunque_venga_de_hablar(dj, monkeypatch):
    monkeypatch.setattr(dj_mod.tts, "available", lambda: True)
    dj._spoke_last[1] = True
    dj.force(1)
    assert dj.should_speak(1) is True


def test_desactivado_no_habla(dj):
    dj.enabled = False
    dj.force(1)
    assert dj.should_speak(1) is False


def test_frecuencia_ronda_un_tercio(dj, monkeypatch):
    monkeypatch.setattr(dj_mod.tts, "available", lambda: True)
    random.seed(1234)
    habla = 0
    for _ in range(3000):
        dj._spoke_last[1] = False   # aislamos la probabilidad de la regla anti-repetición
        if dj.should_speak(1):
            habla += 1
    assert 0.28 < habla / 3000 < 0.39


# --- volumen ----------------------------------------------------------------

def test_arranca_con_el_volumen_por_defecto(dj):
    assert dj.volume == dj_mod.config.DJ_VOLUME_DEFAULT


def test_en_cero_el_dj_se_calla(dj, monkeypatch):
    """Bajar el slider a cero es mutear al DJ, no reproducir silencio."""
    monkeypatch.setattr(dj_mod.tts, "available", lambda: True)
    dj.volume = 0
    dj.force(1)
    assert dj.available() is False
    assert dj.should_speak(1) is False


def test_el_volumen_llega_a_la_sintesis(dj, monkeypatch, tmp_path):
    monkeypatch.setattr(dj_mod.tts, "available", lambda: True)
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    pedidos = []

    async def falso_synthesize(text, voice=None, volume=100):
        pedidos.append(volume)
        return wav

    monkeypatch.setattr(dj_mod.tts, "synthesize", falso_synthesize)
    dj.volume = 45
    dj.force(1)

    asyncio.run(dj.prepare(1, FakeTrack(), session=None))

    assert pedidos == [45]


def test_set_volume_topea_y_devuelve_lo_aplicado(dj):
    assert dj.set_volume(250) == 200
    assert dj.volume == 200
    assert dj.set_volume(-5) == 0
    assert dj.volume == 0


# --- validez del clip -------------------------------------------------------

def test_descarta_la_intro_si_cambio_la_cancion(dj, tmp_path):
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    dj._pending[1] = Clip(path=wav, text="Ahora viene X", for_source="https://x/1", kind="banco")

    assert dj.take(1, FakeTrack(source="https://otra/2")) is None
    assert not wav.exists()   # y el wav se limpia


def test_conserva_el_bumper_aunque_cambie_la_cancion(dj, tmp_path):
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    dj._pending[1] = Clip(path=wav, text="LavaDJ FM", for_source=None, kind="banco")

    clip = dj.take(1, FakeTrack(source="https://otra/2"))
    assert clip is not None
    assert clip.text == "LavaDJ FM"


def test_take_marca_que_hablo(dj, tmp_path):
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    dj._pending[1] = Clip(path=wav, text="hola", for_source=None, kind="banco")
    dj.take(1, FakeTrack())
    assert dj._spoke_last[1] is True


def test_sin_clip_no_marca_que_hablo(dj):
    assert dj.take(1, FakeTrack()) is None
    assert dj._spoke_last[1] is False


def test_drop_limpia_el_wav(dj, tmp_path):
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    dj._pending[1] = Clip(path=wav, text="hola", for_source=None, kind="banco")
    dj.drop(1)
    assert not wav.exists()
    assert 1 not in dj._pending


# --- elección de fuente -----------------------------------------------------

def test_cae_al_banco_si_la_noticia_falla(dj, monkeypatch):
    monkeypatch.setattr(dj, "_pick_source", lambda has_track: "noticia")
    monkeypatch.setattr(dj, "_news", lambda session: _empty())
    texto, _, fuente = asyncio.run(dj.build_text("Tema", "Artista", None))
    assert fuente in DJ.BANK_CATEGORIES
    assert texto


def test_slang_no_nombra_la_cancion(dj, monkeypatch):
    monkeypatch.setattr(dj, "_pick_source", lambda has_track: "slang")
    resultado = asyncio.run(dj.build_text("Tema", "Artista", None))
    assert resultado == ("no aura", False, "slang")


def test_la_intro_reemplaza_titulo_y_artista(dj, monkeypatch):
    monkeypatch.setattr(dj, "_pick_source", lambda has_track: "banco")
    monkeypatch.setattr(random, "choice", lambda seq: "intro" if "intro" in seq else seq[0])
    texto, nombra, _ = asyncio.run(dj.build_text("Around the World", "Daft Punk", None))
    assert "Around the World" in texto and "Daft Punk" in texto
    assert nombra is True


def test_sin_feeds_no_se_elige_noticia(dj):
    dj.feeds = None
    fuentes = {k for k, _ in dj._weights(has_track=True)}
    assert "noticia" not in fuentes


async def _empty():
    return ""


# --- configuración recortada (solo stingers + noticias) ---------------------

def test_sin_banco_no_se_ofrece_banco():
    """Config actual: solo 'slang' en el JSON. Ofrecer 'banco' sería quedarse
    mudo en ese corte."""
    d = DJ()
    d.phrases = {"slang": ["no aura"]}
    d.feeds = object()
    assert {k for k, _ in d._weights(True)} == {"slang", "noticia"}


def test_sin_banco_cae_al_stinger(monkeypatch):
    d = DJ()
    d.phrases = {"slang": ["no aura"]}
    monkeypatch.setattr(d, "_pick_source", lambda has_track: "banco")
    assert asyncio.run(d.build_text("Tema", "Artista", None)) == ("no aura", False, "slang")


def test_el_json_real_tiene_las_frases_del_usuario():
    d = DJ()
    assert len(d.phrases.get("slang", [])) > 90
    assert "no aura" in d.phrases["slang"]


def test_titular_sin_plantilla_se_lee_tal_cual():
    """Sin la clave 'noticia' en el JSON, el titular va plano."""
    d = DJ()
    d.phrases = {"slang": ["x"]}

    class FakeFeeds:
        async def next_headline(self, session):
            return "Alcalde inaugura puente sin río"

    d.feeds = FakeFeeds()
    assert asyncio.run(d._news(None)) == "Alcalde inaugura puente sin río."


def test_el_json_real_tiene_las_frases_de_intermision():
    d = DJ()
    frases = d.phrases.get("intermision", [])
    assert len(frases) > 100
    assert "We lost containment." in frases
    assert d._has_bank()


def test_el_json_no_tiene_basura_de_la_ui_de_discord():
    """Al pegar desde Discord se cuelan botones y marcas de tiempo."""
    d = DJ()
    basura = ("Haz clic para reaccionar", "Añadir reacción", "Responder",
              "Reenviar", ":thumbsup:", ":100:", ":x:")
    for categoria, frases in d.phrases.items():
        for frase in frases:
            assert frase not in basura, f"{categoria}: quedó UI de Discord: {frase!r}"
            assert not frase.startswith("["), f"{categoria}: marca de tiempo: {frase!r}"


# --- voz según idioma -------------------------------------------------------

def test_las_frases_en_ingles_usan_voz_inglesa():
    from core import tts
    assert DJ.voice_for("intermision") == tts.VOICE_EN
    assert DJ.voice_for("slang") == tts.VOICE_EN


def test_los_titulares_usan_voz_espanola():
    from core import tts
    assert DJ.voice_for("noticia") == tts.VOICE_ES
    # Un bumper agregado en castellano no debe salir con voz inglesa.
    assert DJ.voice_for("bumper") == tts.VOICE_ES


def test_build_text_devuelve_la_categoria_real(dj, monkeypatch):
    """El kind tiene que ser la categoría concreta, no un genérico 'banco':
    de ahí sale la elección de voz."""
    monkeypatch.setattr(dj, "_pick_source", lambda has_track: "banco")
    monkeypatch.setattr(random, "choice", lambda seq: "ad" if "ad" in seq else seq[0])
    _, _, kind = asyncio.run(dj.build_text("Tema", "Artista", None))
    assert kind == "ad"


# --- avisos hablados ---------------------------------------------------------

def test_el_aviso_se_sintetiza_con_la_voz_del_dj(dj, monkeypatch, tmp_path):
    """Los clips son wavs locales: es el único canal que sigue vivo cuando
    justo falla lo que hay que avisar."""
    monkeypatch.setattr(dj_mod.tts, "available", lambda: True)
    wav = tmp_path / "aviso.wav"
    wav.write_bytes(b"RIFF")
    dicho = {}

    async def falso_synthesize(text, voice=None, volume=100):
        dicho["texto"] = text
        dicho["voz"] = voice
        return wav

    monkeypatch.setattr(dj_mod.tts, "synthesize", falso_synthesize)

    clip = asyncio.run(dj.aviso("Se me cayó Gato por liebre, sigo con la próxima."))

    assert clip.path == wav
    assert "Gato por liebre" in dicho["texto"]
    assert dicho["voz"] == dj_mod.tts.VOICE_ES


def test_el_aviso_habla_aunque_el_dj_este_apagado(dj, monkeypatch, tmp_path):
    """Apagar el locutor no es apagar las alarmas: `!dj off` saca las noticias
    y los bumpers, no el aviso de que la música se cortó."""
    monkeypatch.setattr(dj_mod.tts, "available", lambda: True)
    wav = tmp_path / "aviso.wav"
    wav.write_bytes(b"RIFF")

    async def falso_synthesize(text, voice=None, volume=100):
        return wav

    monkeypatch.setattr(dj_mod.tts, "synthesize", falso_synthesize)
    dj.enabled = False

    assert asyncio.run(dj.aviso("se cortó todo")) is not None


def test_sin_espeak_el_aviso_no_revienta(dj, monkeypatch):
    monkeypatch.setattr(dj_mod.tts, "available", lambda: False)
    assert asyncio.run(dj.aviso("se cortó todo")) is None
