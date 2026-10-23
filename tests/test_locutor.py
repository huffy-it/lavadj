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
    def __init__(self, temas=(), auto=(), connected=True):
        self.connected = connected
        self.queue = FakeQueue(temas)
        self.auto_queue = FakeQueue(auto)
        self.guild = SimpleNamespace(id=1)


def _locutor(monkeypatch=None, track=None, **dj):
    """Locutor con DJ falso; `_clip_a_track` devuelve `track` y anota los clips en `convertidos`."""
    locutor = Locutor(SimpleNamespace(**dj))
    locutor.convertidos = []

    async def _a_track(clip):
        locutor.convertidos.append(clip)
        return track

    if monkeypatch:
        monkeypatch.setattr(locutor, "_clip_a_track", _a_track)
    return locutor


def _wav(tmp_path, nombre="dj_x.wav"):
    wav = tmp_path / nombre
    wav.write_bytes(b"RIFF")
    return wav


async def _nada(*a, **kw):
    return None


def _corte(wav):
    return lambda g, t: Clip(wav, "hola", None, "bumper")


def _aviso(wav):
    async def aviso(texto):
        return Clip(wav, texto, None, "aviso")
    return aviso


async def _no_se_llama(texto):
    raise AssertionError("el DJ no tenía que hablar")


def test_el_proximo_mira_primero_la_cola_y_despues_el_autoplay():
    pedido, sugerido = FakeTrack("pedida"), FakeTrack("sugerida")
    assert proximo(FakePlayer(temas=[pedido], auto=[sugerido])) is pedido
    assert proximo(FakePlayer(auto=[sugerido])) is sugerido
    assert proximo(FakePlayer()) is None


async def test_si_lavalink_no_carga_el_clip_se_borra_su_wav(tmp_path, monkeypatch):
    wav = _wav(tmp_path)
    siguiente = FakeTrack("proximo")
    locutor = _locutor(monkeypatch, prepare=_nada, take=_corte(wav))

    await locutor.encolar(FakePlayer(temas=[siguiente]), siguiente, None)

    assert not wav.exists()


async def test_si_lavalink_no_carga_el_aviso_se_borra_su_wav(tmp_path, monkeypatch):
    wav = _wav(tmp_path, "dj_aviso.wav")
    await _locutor(monkeypatch, aviso=_aviso(wav)).decir(FakePlayer(), "se cayó")
    assert not wav.exists()


async def test_el_corte_queda_al_frente_delante_del_tema_que_presenta(tmp_path, monkeypatch):
    wav = _wav(tmp_path)
    siguiente, corte = FakeTrack("proximo"), FakeTrack("corte")
    vc = FakePlayer(temas=[siguiente, FakeTrack("otro")])

    async def _prepare(guild_id, para, session):
        assert session == "la-sesion"

    await _locutor(monkeypatch, corte, prepare=_prepare, take=_corte(wav)).encolar(vc, siguiente, "la-sesion")

    assert vc.queue[:2] == [corte, siguiente]
    assert wav.exists()


@pytest.mark.parametrize("conectado, otro_primero", [(False, False), (True, True)], ids=["desconectado", "ya_no_es_el_proximo"])
async def test_un_corte_que_ya_no_vale_se_descarta(tmp_path, monkeypatch, conectado, otro_primero):
    """Mientras se sintetizaba pudo haber un skip, un shuffle o un !stop, o el player se fue."""
    wav = _wav(tmp_path)
    presentado = FakeTrack("presentado")
    vc = FakePlayer(temas=[FakeTrack("otro") if otro_primero else presentado], connected=conectado)
    antes = list(vc.queue)
    locutor = _locutor(monkeypatch, prepare=_nada, take=_corte(wav))

    await locutor.encolar(vc, presentado, None)

    assert not wav.exists()
    assert list(vc.queue) == antes
    assert locutor.convertidos == []


async def test_el_aviso_va_al_frente_de_la_cola(tmp_path, monkeypatch):
    wav = _wav(tmp_path, "dj_aviso.wav")
    locucion, sigue = FakeTrack("aviso"), FakeTrack("la que sigue")
    vc = FakePlayer(temas=[sigue])

    await _locutor(monkeypatch, locucion, aviso=_aviso(wav)).decir(vc, "se cayó")

    assert list(vc.queue) == [locucion, sigue]
    assert wav.exists()


@pytest.mark.parametrize("conectado, frase", [(True, None), (True, ""), (False, "se cayó")])
async def test_sin_frase_o_sin_player_conectado_el_dj_se_calla(conectado, frase):
    vc = FakePlayer(connected=conectado)
    await _locutor(aviso=_no_se_llama).decir(vc, frase)
    assert list(vc.queue) == []


def test_un_wav_del_cache_es_un_corte_y_una_cancion_no(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    corte, por_identifier, cancion = FakeTrack("corte"), FakeTrack("otro corte"), FakeTrack("tema")
    corte.uri = str(tmp_path / "dj_abc.wav")
    por_identifier.identifier = str(tmp_path / "dj_def.wav")
    cancion.uri = str(tmp_path.parent / "cancion.wav")

    assert Locutor.es_clip(corte) and Locutor.es_clip(por_identifier)
    assert not Locutor.es_clip(cancion)


def test_borrar_el_corte_saca_su_wav_y_deja_la_cancion(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    wav = _wav(tmp_path, "dj_abc.wav")
    ajeno = tmp_path.parent / "cancion.wav"
    ajeno.write_bytes(b"RIFF")
    corte, cancion = FakeTrack("corte"), FakeTrack("tema")
    corte.uri, cancion.uri = str(wav), str(ajeno)
    locutor = _locutor()

    locutor.borrar_clip(corte)
    locutor.borrar_clip(cancion)

    assert not wav.exists() and ajeno.exists()
