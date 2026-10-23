import random

import pytest

from core import dj as dj_mod
from core import tts
from core.dj import DJ, Clip, sanitize
from core.guionista import Guionista

SLANG = ("no aura", False, "slang")


class FakeTrack:
    def __init__(self, title="Daft Punk - Around the World", source="https://x/1"):
        self.title = title
        self.source = source


class FakeFeeds:
    async def next_headline(self, session):
        return "Alcalde inaugura puente sin río"


@pytest.fixture(autouse=True)
def sin_guionista(monkeypatch):
    """Un `.env` con el guionista encendido no puede llevar los tests a la API real."""
    monkeypatch.setattr(dj_mod.config, "DJ_GUIONISTA", "frases")


@pytest.fixture
def dj():
    instance = DJ()
    instance.phrases = {   # que los tests no dependan del JSON real
        "bumper": ["Estás escuchando LavaDJ FM."],
        "ad": ["Auspicia la deuda."],
        "outro": ["Eso fue lo que fue."],
        "intro": ["Ahora viene {titulo}, de {artista}."],
        "noticia": ["Boletín. {titular}. Sin comentarios."],
        "slang": ["no aura"],
    }
    instance.enabled = True
    return instance


@pytest.fixture
def wav(tmp_path):
    ruta = tmp_path / "clip.wav"
    ruta.write_bytes(b"RIFF")
    return ruta


@pytest.fixture
def sintesis(monkeypatch, wav):
    """`tts.synthesize` falso: anota (texto, voz, volumen) de cada pedido y devuelve un wav."""
    pedidos = []

    async def falso_synthesize(text, voice=None, volume=100):
        pedidos.append((text, voice, volume))
        return wav
    monkeypatch.setattr(dj_mod.tts, "available", lambda: True)
    monkeypatch.setattr(dj_mod.tts, "synthesize", falso_synthesize)
    return pedidos


def elegir_fuente(monkeypatch, dj, fuente):
    monkeypatch.setattr(dj, "_pick_source", lambda has_track: fuente)


def test_sanitize_saca_urls_emoji_y_saltos_de_linea():
    assert sanitize("Mira esto https://ejemplo.com/x 🎵 ahora") == "Mira esto ahora"
    assert sanitize("una\n\nlínea\tsola") == "una línea sola"


def test_sanitize_corta_en_frase_completa_si_hay_punto_pasada_la_mitad():
    assert sanitize("x" * 150 + ". " + "y " * 100) == "x" * 150 + "."


@pytest.mark.parametrize("texto, termina_en_punto", [("palabra " * 100, False), ("y " * 200, True)])
def test_sanitize_respeta_el_tope_tambien_con_corte_duro(texto, termina_en_punto):
    out = sanitize(texto)
    assert len(out) <= dj_mod.MAX_CHARS
    assert out.endswith(".") or not termina_en_punto


def test_no_habla_dos_veces_seguidas(dj):
    dj._spoke_last[1] = True
    random.seed(0)   # aunque el dado diga que sí
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
        habla += dj.should_speak(1)
    assert 0.28 < habla / 3000 < 0.39


def test_el_volumen_arranca_en_el_defecto_y_set_volume_topea(dj):
    assert dj.volume == dj_mod.config.DJ_VOLUME_DEFAULT
    assert dj.set_volume(250) == 200 and dj.volume == 200
    assert dj.set_volume(-5) == 0 and dj.volume == 0


def test_en_cero_el_dj_se_calla(dj, monkeypatch):
    """Bajar el slider a cero es mutear al DJ, no reproducir silencio."""
    monkeypatch.setattr(dj_mod.tts, "available", lambda: True)
    dj.volume = 0
    dj.force(1)
    assert dj.available() is False
    assert dj.should_speak(1) is False


async def test_el_volumen_llega_a_la_sintesis(dj, sintesis):
    dj.volume = 45
    dj.force(1)
    await dj.prepare(1, FakeTrack(), session=None)
    assert [volumen for *_, volumen in sintesis] == [45]


def test_descarta_la_intro_si_cambio_la_cancion_y_limpia_el_wav(dj, wav):
    dj._pending[1] = Clip(path=wav, text="Ahora viene X", for_source="https://x/1", kind="banco")
    assert dj.take(1, FakeTrack(source="https://otra/2")) is None
    assert not wav.exists()


def test_conserva_el_bumper_aunque_cambie_la_cancion(dj, wav):
    dj._pending[1] = Clip(path=wav, text="LavaDJ FM", for_source=None, kind="banco")
    clip = dj.take(1, FakeTrack(source="https://otra/2"))
    assert clip is not None and clip.text == "LavaDJ FM"
    assert dj._spoke_last[1] is True   # take marca que habló


def test_sin_clip_no_marca_que_hablo(dj):
    assert dj.take(1, FakeTrack()) is None
    assert dj._spoke_last[1] is False


def test_drop_limpia_el_wav(dj, wav):
    dj._pending[1] = Clip(path=wav, text="hola", for_source=None, kind="banco")
    dj.drop(1)
    assert not wav.exists() and 1 not in dj._pending


async def test_cae_al_banco_si_la_noticia_falla(dj, monkeypatch):
    async def vacia():
        return ""
    elegir_fuente(monkeypatch, dj, "noticia")
    monkeypatch.setattr(dj, "_news", lambda session: vacia())
    texto, _, fuente = await dj.build_text("Tema", "Artista", None)
    assert fuente in DJ.BANK_CATEGORIES and texto


async def test_slang_no_nombra_la_cancion(dj, monkeypatch):
    elegir_fuente(monkeypatch, dj, "slang")
    assert await dj.build_text("Tema", "Artista", None) == SLANG


@pytest.mark.parametrize("categoria, nombra", [("intro", True), ("ad", False)])
async def test_build_text_devuelve_la_categoria_real_y_la_intro_nombra_titulo_y_artista(dj, monkeypatch, categoria, nombra):
    """El kind es la categoría concreta, no un genérico 'banco': de ahí sale la voz."""
    elegir_fuente(monkeypatch, dj, "banco")
    monkeypatch.setattr(random, "choice", lambda seq: categoria if categoria in seq else seq[0])
    texto, nombra_, kind = await dj.build_text("Around the World", "Daft Punk", None)
    assert kind == categoria and nombra_ is nombra
    assert ("Around the World" in texto and "Daft Punk" in texto) == nombra


def test_sin_feeds_no_se_elige_noticia(dj):
    dj.feeds = None
    assert "noticia" not in {k for k, _ in dj._weights(has_track=True)}


def test_sin_banco_no_se_ofrece_banco():
    """Config actual: solo 'slang' en el JSON; ofrecer 'banco' sería quedarse mudo en ese corte."""
    d = DJ()
    d.phrases = {"slang": ["no aura"]}
    d.feeds = object()
    assert {k for k, _ in d._weights(True)} == {"slang", "noticia"}


async def test_sin_banco_cae_al_stinger(monkeypatch):
    d = DJ()
    d.phrases = {"slang": ["no aura"]}
    monkeypatch.setattr(d, "_pick_source", lambda has_track: "banco")
    assert await d.build_text("Tema", "Artista", None) == SLANG


async def test_titular_sin_plantilla_se_lee_tal_cual():
    d = DJ()
    d.phrases = {"slang": ["x"]}
    d.feeds = FakeFeeds()
    assert await d._news(None) == "Alcalde inaugura puente sin río."


def test_el_json_real_tiene_las_frases_del_usuario_y_las_de_intermision():
    d = DJ()
    assert len(d.phrases.get("slang", [])) > 90 and "no aura" in d.phrases["slang"]
    frases = d.phrases.get("intermision", [])
    assert len(frases) > 100 and "We lost containment." in frases
    assert d._has_bank()


def test_el_json_no_tiene_basura_de_la_ui_de_discord():
    """Al pegar desde Discord se cuelan botones y marcas de tiempo."""
    basura = ("Haz clic para reaccionar", "Añadir reacción", "Responder", "Reenviar", ":thumbsup:", ":100:", ":x:")
    for categoria, frases in DJ().phrases.items():
        for frase in frases:
            assert frase not in basura, f"{categoria}: quedó UI de Discord: {frase!r}"
            assert not frase.startswith("["), f"{categoria}: marca de tiempo: {frase!r}"


@pytest.mark.parametrize("fuente, voz", [
    ("intermision", "VOICE_EN"), ("slang", "VOICE_EN"),
    ("noticia", "VOICE_ES"), ("bumper", "VOICE_ES"), ("guion", "VOICE_ES"),   # un bumper en castellano no sale en inglés
])
def test_la_voz_depende_de_la_fuente(fuente, voz):
    assert DJ.voice_for(fuente) == getattr(tts, voz)


async def test_el_aviso_se_sintetiza_con_la_voz_del_dj_aunque_este_apagado(dj, sintesis, wav):
    """Es el único canal que sigue vivo cuando falla lo que hay que avisar: `!dj off` no lo apaga."""
    dj.enabled = False
    clip = await dj.aviso("Se me cayó Gato por liebre, sigo con la próxima.")
    assert clip.path == wav
    (texto, voz, _), = sintesis
    assert "Gato por liebre" in texto and voz == tts.VOICE_ES


async def test_sin_espeak_el_aviso_no_revienta(dj, monkeypatch):
    monkeypatch.setattr(dj_mod.tts, "available", lambda: False)
    assert await dj.aviso("se cortó todo") is None


@pytest.fixture
def guion(dj, monkeypatch):
    """El DJ con el guionista de Claude encendido y `escribir` suplantado: ninguna llamada sale de aquí."""
    dj.guionista.elegir("claude")
    dj.guionista.pedidos = []
    dj.guionista.respuesta = "Ahora suena Daft Punk, que lo disfrutes."

    async def falso_escribir(titulo, **datos):
        dj.guionista.pedidos.append({"titulo": titulo, **datos})
        if isinstance(dj.guionista.respuesta, Exception):
            raise dj.guionista.respuesta
        return dj.guionista.respuesta
    monkeypatch.setattr(dj.guionista, "escribir", falso_escribir)
    return dj


async def test_con_guionista_se_usa_su_texto_y_la_fuente_es_guion(guion):
    resultado = await guion.build_text("Around the World", "Daft Punk", None, "Ana")
    assert resultado == ("Ahora suena Daft Punk, que lo disfrutes.", True, "guion")
    (pedido,) = guion.guionista.pedidos
    assert (pedido["titulo"], pedido["artista"], pedido["pidio"]) == ("Around the World", "Daft Punk", "Ana")
    assert pedido["max_chars"] == dj_mod.MAX_CHARS


@pytest.mark.parametrize("respuesta", [None, RuntimeError("se rompió")], ids=["none", "revienta"])
async def test_si_el_guionista_falla_se_cae_al_banco(guion, monkeypatch, respuesta):
    guion.guionista.respuesta = respuesta
    elegir_fuente(monkeypatch, guion, "slang")
    assert await guion.build_text("Tema", "Artista", None) == SLANG
    assert len(guion.guionista.pedidos) == 1   # lo intentó primero


async def test_con_el_guionista_apagado_no_se_lo_llama_nunca(dj, monkeypatch):
    llamadas = []

    async def no_deberia(*args, **kwargs):
        llamadas.append(1)
    monkeypatch.setattr(dj.guionista, "escribir", no_deberia)
    elegir_fuente(monkeypatch, dj, "slang")
    assert dj.guionista.activo == "frases"
    assert await dj.build_text("Tema", "Artista", None) == SLANG
    assert llamadas == []


async def test_sin_titulo_no_se_llama_al_guionista(guion, monkeypatch):
    elegir_fuente(monkeypatch, guion, "slang")
    await guion.build_text("", "", None)
    assert guion.guionista.pedidos == []


async def test_prepare_con_guionista_sintetiza_con_voz_espanola_y_liga_el_clip_a_la_cancion(guion, sintesis):
    guion.guionista.respuesta = "Va Daft Punk 🎵 https://x.com/y con Ana."
    track = FakeTrack()
    track.requester = "Ana"
    guion.force(1)
    await guion.prepare(1, track, session=None)
    assert [(texto, voz) for texto, voz, _ in sintesis] == [("Va Daft Punk con Ana.", tts.VOICE_ES)]   # pasó por sanitize
    clip = guion._pending[1]
    assert clip.kind == "guion"
    assert clip.for_source == "https://x/1"   # nombra la canción: si hay skip, se descarta
    pedido = guion.guionista.pedidos[0]
    assert (pedido["artista"], pedido["pidio"]) == ("Daft Punk", "Ana")


async def test_el_guion_largo_se_corta_al_tope(guion, sintesis):
    guion.guionista.respuesta = "palabra " * 100
    guion.force(1)
    await guion.prepare(1, FakeTrack(), session=None)
    assert len(sintesis[0][0]) <= dj_mod.MAX_CHARS


async def test_el_titular_va_al_guionista_solo_a_veces(guion, monkeypatch):
    guion.feeds = FakeFeeds()
    monkeypatch.setattr(random, "random", lambda: 0.0)
    await guion.build_text("Tema", "Artista", None)
    monkeypatch.setattr(random, "random", lambda: 0.99)
    await guion.build_text("Tema", "Artista", None)
    con, sin = guion.guionista.pedidos
    assert con["titular"] == "Alcalde inaugura puente sin río" and sin["titular"] == ""


async def test_sin_feeds_el_guionista_no_recibe_titular(guion, monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.0)
    guion.feeds = None
    await guion.build_text("Tema", "Artista", None)
    assert guion.guionista.pedidos[0]["titular"] == ""


def test_el_dj_acepta_un_guionista_inyectado_y_por_defecto_arranca_sin_guionista():
    g = Guionista("ollama")
    assert DJ(guionista=g).guionista is g
    d = DJ()
    assert d.guionista.activo == "frases" and not d.guionista.encendido
