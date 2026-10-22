"""El DJ frente al player: qué pasa con sus wavs y su lugar en la cola."""

from types import SimpleNamespace

import pytest

from core import tts
from core.dj import Clip
from core.locutor import Locutor, proximo


class FakeTrack:
    def __init__(self, title="Un tema"):
        self.title = title
        self.identifier = title


class FakeQueue(list):
    def put_at(self, index, track):
        self.insert(index, track)


class FakePlayer:
    def __init__(self, temas=(), auto=()):
        self.connected = True
        self.queue = FakeQueue(temas)
        self.auto_queue = FakeQueue(auto)
        self.guild = SimpleNamespace(id=1)


def _locutor(**dj):
    return Locutor(SimpleNamespace(**dj))


def _wav(tmp_path, nombre="dj_x.wav"):
    wav = tmp_path / nombre
    wav.write_bytes(b"RIFF")
    return wav


def test_el_proximo_mira_primero_la_cola_y_despues_el_autoplay():
    pedido, sugerido = FakeTrack("pedida"), FakeTrack("sugerida")
    assert proximo(FakePlayer(temas=[pedido], auto=[sugerido])) is pedido
    assert proximo(FakePlayer(auto=[sugerido])) is sugerido
    assert proximo(FakePlayer()) is None


# --- wavs del DJ que no se quedan en disco ---------------------------------------

async def test_si_lavalink_no_carga_el_clip_se_borra_su_wav(tmp_path, monkeypatch):
    wav = _wav(tmp_path)
    siguiente = FakeTrack("proximo")
    vc = FakePlayer(temas=[siguiente])

    async def _prepare(*a, **kw):
        return None

    async def _sin_track(self, clip):
        return None

    locutor = _locutor(prepare=_prepare, take=lambda g, t: Clip(wav, "hola", None, "bumper"))
    monkeypatch.setattr(Locutor, "_clip_a_track", _sin_track)

    await locutor.encolar(vc, siguiente, None)

    assert not wav.exists()


async def test_si_lavalink_no_carga_el_aviso_se_borra_su_wav(tmp_path, monkeypatch):
    wav = _wav(tmp_path, "dj_aviso.wav")

    async def _aviso(texto):
        return Clip(wav, texto, None, "aviso")

    async def _sin_track(self, clip):
        return None

    locutor = _locutor(aviso=_aviso)
    monkeypatch.setattr(Locutor, "_clip_a_track", _sin_track)

    await locutor.decir(FakePlayer(), "se cayó")

    assert not wav.exists()


# --- el corte entra a la cola ------------------------------------------------------

async def test_el_corte_queda_al_frente_delante_del_tema_que_presenta(tmp_path, monkeypatch):
    wav = _wav(tmp_path)
    siguiente, corte = FakeTrack("proximo"), FakeTrack("corte")
    vc = FakePlayer(temas=[siguiente, FakeTrack("otro")])

    async def _prepare(guild_id, para, session):
        assert session == "la-sesion"

    async def _a_track(self, clip):
        return corte

    locutor = _locutor(prepare=_prepare, take=lambda g, t: Clip(wav, "hola", None, "bumper"))
    monkeypatch.setattr(Locutor, "_clip_a_track", _a_track)

    await locutor.encolar(vc, siguiente, "la-sesion")

    assert vc.queue[:2] == [corte, siguiente]
    assert wav.exists()


async def test_si_el_tema_ya_no_es_el_proximo_el_corte_se_descarta(tmp_path, monkeypatch):
    """Mientras se sintetizaba pudo haber un skip, un shuffle o un !stop."""
    wav = _wav(tmp_path)
    presentado, otro = FakeTrack("presentado"), FakeTrack("otro")
    vc = FakePlayer(temas=[otro])

    async def _prepare(*a, **kw):
        return None

    async def _no_se_llama(self, clip):
        raise AssertionError("no tenía que cargar un corte que ya no vale")

    locutor = _locutor(prepare=_prepare, take=lambda g, t: Clip(wav, "hola", None, "bumper"))
    monkeypatch.setattr(Locutor, "_clip_a_track", _no_se_llama)

    await locutor.encolar(vc, presentado, None)

    assert not wav.exists()
    assert list(vc.queue) == [otro]


async def test_si_el_player_ya_no_esta_conectado_el_corte_se_descarta(tmp_path):
    wav = _wav(tmp_path)
    siguiente = FakeTrack("proximo")
    vc = FakePlayer(temas=[siguiente])
    vc.connected = False

    async def _prepare(*a, **kw):
        return None

    locutor = _locutor(prepare=_prepare, take=lambda g, t: Clip(wav, "hola", None, "bumper"))

    await locutor.encolar(vc, siguiente, None)

    assert not wav.exists()
    assert list(vc.queue) == [siguiente]


async def test_el_aviso_va_al_frente_de_la_cola(tmp_path, monkeypatch):
    wav = _wav(tmp_path, "dj_aviso.wav")
    locucion, sigue = FakeTrack("aviso"), FakeTrack("la que sigue")
    vc = FakePlayer(temas=[sigue])

    async def _aviso(texto):
        return Clip(wav, texto, None, "aviso")

    async def _a_track(self, clip):
        return locucion

    locutor = _locutor(aviso=_aviso)
    monkeypatch.setattr(Locutor, "_clip_a_track", _a_track)

    await locutor.decir(vc, "se cayó")

    assert list(vc.queue) == [locucion, sigue]
    assert wav.exists()


@pytest.mark.parametrize("frase", [None, ""])
async def test_sin_frase_el_dj_se_calla(frase):
    async def _aviso(texto):
        raise AssertionError("no hay nada que decir")

    await _locutor(aviso=_aviso).decir(FakePlayer(), frase)


async def test_con_el_player_desconectado_el_dj_no_habla():
    async def _aviso(texto):
        raise AssertionError("hablarle a nadie es gastar espeak")

    vc = FakePlayer()
    vc.connected = False

    await _locutor(aviso=_aviso).decir(vc, "se cayó")

    assert list(vc.queue) == []


# --- reconocer y borrar los cortes ---------------------------------------------------

def test_un_wav_del_cache_es_un_corte_y_una_cancion_no(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    corte, por_identifier, cancion = FakeTrack("corte"), FakeTrack("otro corte"), FakeTrack("tema")
    corte.uri = str(tmp_path / "dj_abc.wav")
    por_identifier.identifier = str(tmp_path / "dj_def.wav")
    cancion.uri = str(tmp_path.parent / "cancion.wav")

    assert Locutor.es_clip(corte)
    assert Locutor.es_clip(por_identifier)
    assert not Locutor.es_clip(cancion)


def test_borrar_el_corte_saca_su_wav_y_deja_la_cancion(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    wav = _wav(tmp_path, "dj_abc.wav")
    ajeno = tmp_path.parent / "cancion.wav"
    ajeno.write_bytes(b"RIFF")
    corte, cancion = FakeTrack("corte"), FakeTrack("tema")
    corte.uri = str(wav)
    cancion.uri = str(ajeno)
    locutor = _locutor()

    locutor.borrar_clip(corte)
    locutor.borrar_clip(cancion)

    assert not wav.exists()
    assert ajeno.exists()
