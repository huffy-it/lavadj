"""Pruebas contra el Lavalink y el yt-dlp de verdad.

Están excluidas de `pytest` a secas (ver `pytest.ini`): necesitan red, un Lavalink
arriba y tardan segundos. Se corren a propósito:

    .venv/bin/python -m pytest -m integracion              # Linux
    .venv\\Scripts\\python.exe -m pytest -m integracion     # Windows

Corren en la máquina del dueño y no en CI: YouTube bloquea las IP de centros de
datos, así que en un runner fallarían por motivos que no son del bot.

Cada test se salta, con el motivo, si falta Lavalink o yt-dlp: sin ellos no hay
nada que comprobar y fallar solo taparía el aviso útil.

Esto NO prueba que el audio suene. Eso lo hace `scripts/probar-que-suena.py`, que
entra a un canal de voz. Aquí se comprueban las tres mitades que más se rompen, cada
una por separado, para saber cuál es la culpable.
"""

import shutil
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp
import pytest

from core import config

pytestmark = pytest.mark.integracion

# El mismo video con el que AGENTS.md prueba a mano.
VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

TIMEOUT_REST = aiohttp.ClientTimeout(total=60)


@pytest.fixture(scope="module")
def lavalink():
    """Se salta el test si Lavalink no contesta; si contesta, no devuelve nada."""
    peticion = urllib.request.Request(
        f"{config.LAVALINK_URI.rstrip('/')}/version",
        headers={"Authorization": config.LAVALINK_PASSWORD},
    )
    # Sin proxy: un proxy del sistema no debe interponerse entre nosotros y 127.0.0.1.
    abridor = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with abridor.open(peticion, timeout=3):
            return
    except urllib.error.HTTPError as error:
        if error.code == 401:
            pytest.skip(
                f"Lavalink en {config.LAVALINK_URI} rechazó la contraseña "
                "(revisa LAVALINK_PASSWORD en el .env)"
            )
        pytest.skip(f"Lavalink en {config.LAVALINK_URI} contestó HTTP {error.code} a /version")
    except (urllib.error.URLError, OSError, ValueError) as error:
        pytest.skip(f"Lavalink no contesta en {config.LAVALINK_URI}: {error}")


@pytest.fixture(scope="module")
def ytdlp():
    """Se salta el test si no se encuentra yt-dlp."""
    # `which` también acepta una ruta absoluta; el `is_file` cubre un binario que
    # existe pero que Windows no reconoce como ejecutable por su extensión.
    if not (shutil.which(config.YTDLP_BIN) or Path(config.YTDLP_BIN).is_file()):
        pytest.skip(f"no encuentro yt-dlp ({config.YTDLP_BIN!r}); instálalo o define YTDLP_BIN")


async def _loadtracks(identificador: str) -> dict:
    """GET /v4/loadtracks, la misma consulta REST que hace el bot (vía wavelink)."""
    async with aiohttp.ClientSession(timeout=TIMEOUT_REST) as sesion:
        # `params` codifica el identificador: una URL de googlevideo trae `?`, `&` y `=`.
        async with sesion.get(
            f"{config.LAVALINK_URI.rstrip('/')}/v4/loadtracks",
            params={"identifier": identificador},
            headers={"Authorization": config.LAVALINK_PASSWORD},
        ) as respuesta:
            assert respuesta.status == 200, f"loadtracks contestó HTTP {respuesta.status}"
            return await respuesta.json()


async def test_ytdlp_entrega_una_url_directa_del_audio(lavalink, ytdlp):
    # Se importa aquí y no arriba para que la colección de tests no dependa de
    # que `core.resolver` esté entero.
    from core.resolver import Resolver

    url = await Resolver().direct_url(VIDEO)

    assert url, "yt-dlp no entregó ninguna URL (¿YouTube cerró el cliente? ver AGENTS.md)"
    assert url.startswith("https://"), f"se esperaba una URL https y llegó: {url[:80]}"


async def test_lavalink_abre_la_url_que_entrega_ytdlp(lavalink, ytdlp):
    # Esto no prueba la reproducción completa (de eso se encarga
    # scripts/probar-que-suena.py, que entra a voz), pero sí las dos mitades que más
    # se rompen: que yt-dlp siga resolviendo y que la fuente `http` de Lavalink
    # pueda abrir lo que yt-dlp le entrega.
    from core.resolver import Resolver

    url = await Resolver().direct_url(VIDEO)
    assert url, "yt-dlp no entregó ninguna URL (¿YouTube cerró el cliente? ver AGENTS.md)"

    datos = await _loadtracks(url)

    assert datos["loadType"] == "track", (
        f"Lavalink no pudo abrir la URL de yt-dlp: loadType={datos['loadType']!r}, "
        f"datos={str(datos.get('data'))[:300]}"
    )


async def test_la_busqueda_que_usa_el_bot_devuelve_resultados(lavalink):
    # `ytmsearch` es la fuente por defecto de wavelink, o sea la que usa el bot. No
    # necesita yt-dlp: si falla, el culpable es la búsqueda del plugin de Lavalink.
    datos = await _loadtracks("ytmsearch:rick astley never gonna give you up")

    assert datos["loadType"] == "search", (
        f"la búsqueda no devolvió resultados: loadType={datos['loadType']!r}, "
        f"datos={str(datos.get('data'))[:300]}"
    )
    assert datos["data"], "la búsqueda contestó 'search' pero sin ninguna pista"
