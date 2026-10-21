"""Spotify como índice: de un link salen nombres, no audio.

El HTML de prueba es el mismo esqueleto que sirve el reproductor embebible,
recortado a lo que el parser realmente mira.
"""

import json

import pytest

from core import spotify


def envolver(entidad: dict) -> str:
    datos = {"props": {"pageProps": {"state": {"data": {"entity": entidad}}}}}
    return (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(datos)
        + "</script></body></html>"
    )


PLAYLIST = envolver({
    "type": "playlist",
    "name": "Fiesta",
    "trackList": [
        {"title": "Despacito", "subtitle": "Luis Fonsi, Daddy Yankee"},
        {"title": "Danza Kuduro", "subtitle": "Don Omar"},
    ],
})

TRACK_SUELTO = envolver({
    "type": "track",
    "name": "Despacito",
    "title": "Despacito",
    "subtitle": None,
    "artists": [{"name": "Luis Fonsi"}, {"name": "Daddy Yankee"}],
})


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, body=PLAYLIST, status=200):
        self.body = body
        self.status = status
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return FakeResponse(self.body, self.status)


@pytest.mark.parametrize("texto", [
    "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
    "https://open.spotify.com/intl-es/album/4aawyAB9vmqN3uQ7FjRGTy?si=x",
    "https://open.spotify.com/track/6habFhsOp2NvshLv26DqMb",
    "spotify:playlist:37i9dQZEVXbNFJfN1Vw8d9",
    "pon esto https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy gracias",
])
def test_reconoce_los_links_de_spotify(texto):
    assert spotify.es_spotify(texto)


@pytest.mark.parametrize("texto", [
    "despacito", "", "https://www.youtube.com/watch?v=kJQP7kiw5Fk",
    "https://open.spotify.com/", "spotify es una empresa",
])
def test_no_confunde_otras_cosas_con_spotify(texto):
    assert not spotify.es_spotify(texto)


def test_la_consulta_lleva_el_artista_adelante():
    # Con el artista adelante los covers y los karaokes quedan más abajo.
    assert spotify._consulta("Despacito", "Luis Fonsi") == "Luis Fonsi - Despacito"


def test_la_consulta_se_queda_con_el_primer_artista():
    assert spotify._consulta("Dai Dai", "Shakira, Burna Boy") == "Shakira - Dai Dai"


def test_la_consulta_sin_artista_es_solo_el_titulo():
    assert spotify._consulta("Despacito", "") == "Despacito"


def test_parsea_una_playlist():
    lista = spotify.parse_embed(PLAYLIST, limit=10)
    assert lista.name == "Fiesta"
    assert lista.queries == ["Luis Fonsi - Despacito", "Don Omar - Danza Kuduro"]
    assert not lista.truncada


def test_un_tema_suelto_saca_el_artista_de_artists():
    """El embed de un track no trae `subtitle`: el artista está en `artists`."""
    lista = spotify.parse_embed(TRACK_SUELTO, limit=10)
    assert lista.queries == ["Luis Fonsi - Despacito"]


def test_respeta_el_tope_y_lo_avisa():
    lista = spotify.parse_embed(PLAYLIST, limit=1)
    assert lista.queries == ["Luis Fonsi - Despacito"]
    assert lista.truncada


def test_html_sin_json_no_revienta():
    assert spotify.parse_embed("<html>nada por aquí</html>", limit=10) is None


def test_json_con_otra_forma_no_revienta():
    assert spotify.parse_embed(envolver({"type": "playlist"}), limit=10) is None


@pytest.mark.asyncio
async def test_fetch_pega_contra_el_embed_publico():
    session = FakeSession()
    lista = await spotify.fetch(session, "https://open.spotify.com/playlist/abc123?si=x")
    assert lista.kind == "playlist"
    assert len(lista.queries) == 2
    # No pega contra la página normal ni contra la API: solo contra el embed.
    assert session.urls == ["https://open.spotify.com/embed/playlist/abc123"]


@pytest.mark.asyncio
async def test_fetch_de_un_link_que_no_es_spotify_es_none():
    session = FakeSession()
    assert await spotify.fetch(session, "https://youtube.com/watch?v=x") is None
    assert session.urls == []


@pytest.mark.asyncio
async def test_fetch_con_error_de_spotify_es_none():
    """Una lista privada o borrada da 404: se devuelve None, no una excepción."""
    assert await spotify.fetch(FakeSession(body="", status=404), "spotify:playlist:x") is None
