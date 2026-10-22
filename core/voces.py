"""Voces neuronales de Piper: catálogo curado, descarga e instaladas.

Solo se baja lo que está en el catálogo, de una URL fija al tag v1.0.0 de
rhasspy/piper-voices, y cada archivo se verifica con su md5. Piper corre como
subproceso (core/tts.py): aquí no se importa, solo se comprueba que esté instalado.
"""

import asyncio
import contextlib
import hashlib
import importlib.util
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from core import ajustes, config

log = logging.getLogger("voces")

ESPEAK = "espeak"
BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve"
REV = "v1.0.0"
# El .onnx.json de ald en v1.0.0 trae "PhonemeType.ESPEAK", que piper-tts no lee: se fija al commit que lo corrigió.
REV_ALD_CONFIG = "0622afc867cf0388684853ecdf59a498b489949d"
PAISES = {"es_AR": "Argentina", "es_ES": "España", "es_MX": "México"}


class ErrorDeVoz(Exception):
    """Algo que el usuario pidió y no se puede (el mensaje se le muestra tal cual)."""


@dataclass(frozen=True)
class Voz:
    """Una voz del catálogo: el id es `<país>-<nombre>-<calidad>`; espeak no trae md5."""

    id: str
    tamano: int = 0
    md5_modelo: str = ""
    md5_config: str = ""
    rev_config: str = REV

    @property
    def es_piper(self) -> bool:
        return bool(self.md5_modelo)

    def _parte(self, i: int) -> str:
        return self.id.split("-")[i] if self.es_piper else ""

    @property
    def pais(self) -> str:
        return PAISES.get(self._parte(0), "")

    @property
    def nombre(self) -> str:
        return self._parte(1)

    @property
    def calidad(self) -> str:
        return self._parte(2)

    def _url(self, rev: str, sufijo: str) -> str:
        return f"{BASE_URL}/{rev}/es/{self._parte(0)}/{self.nombre}/{self.calidad}/{self.id}{sufijo}"

    @property
    def url_modelo(self) -> str:
        return self._url(REV, ".onnx")

    @property
    def url_config(self) -> str:
        return self._url(self.rev_config, ".onnx.json")


# Tamaño y md5 salen del voices.json del tag v1.0.0; el tamaño es el del .onnx.
CATALOGO = {v.id: v for v in (
    Voz(ESPEAK),
    Voz("es_AR-daniela-high", 114199011, "e373fb657c93877dbc438badeadff4cb", "b68d699a1779d98ee94c0cd9a08ba95b"),
    Voz("es_ES-carlfm-x_low", 28130791, "4137b5aee01ea6241080fc4dbe59a8ee", "f4ab058cc8b5d024f1b123d06049df22"),
    Voz("es_ES-davefx-medium", 63201294, "dc515cd4ecc5f6f72fe14a941188fc9c", "dd157b5eaf6930bf949cf416d9a9307a"),
    Voz("es_ES-mls_10246-low", 63104526, "ab8e93c9d2714fd4481fbca4e2a38891", "8948a322c81cae2bc5fb1b9bbbc4961e"),
    Voz("es_ES-mls_9972-low", 63104526, "587f2fc38dc3f582e771c3748465e2a2", "97195bce39639d77af82c9c9c2110620"),
    Voz("es_ES-sharvard-medium", 76733615, "77e6f9c26e92799fb04bb90b46bf1834", "e49cab310ce362145f7d94b347c291ff"),
    Voz("es_MX-ald-medium", 63201294, "86374058e59b41ac3b7fe4181e1daad6", "aedc07cd37e61360144f04aebb05f7dd", REV_ALD_CONFIG),
    Voz("es_MX-claude-high", 63122309, "cb1966e0ff20ca3aa010f6c9a0ce296a", "36882201922fabb409f18d284af3eddd"),
)}


def piper_disponible() -> bool:
    """Si piper-tts está instalado. Se busca sin importarlo: es GPL y solo corre como subproceso."""
    return importlib.util.find_spec("piper") is not None


def ruta_modelo(voz_id: str) -> Path:
    return config.CARPETA_DE_VOCES / f"{voz_id}.onnx"


def ruta_config(voz_id: str) -> Path:
    return config.CARPETA_DE_VOCES / f"{voz_id}.onnx.json"


def instalada(voz_id: str) -> bool:
    """Está instalada si están los dos archivos; espeak viene con el sistema."""
    voz = CATALOGO.get(voz_id)
    if voz is None:
        return False
    return not voz.es_piper or (ruta_modelo(voz_id).is_file() and ruta_config(voz_id).is_file())


def instaladas() -> list[str]:
    """Los ids de las voces de Piper que se pueden usar."""
    return [v.id for v in CATALOGO.values() if v.es_piper and instalada(v.id)]


def _md5(ruta: Path) -> str:
    resumen = hashlib.md5()
    with open(ruta, "rb") as archivo:
        for trozo in iter(lambda: archivo.read(1 << 20), b""):
            resumen.update(trozo)
    return resumen.hexdigest()


def _borrar_temporal(ruta: Path) -> None:
    with contextlib.suppress(OSError):
        ruta.unlink(missing_ok=True)


async def _bajar(url: str, destino: Path, al_avanzar=None) -> None:
    """Baja `url` a `destino`; `al_avanzar(bytes_hechos)` avisa el progreso."""
    espera = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30)
    async with aiohttp.ClientSession(timeout=espera) as sesion, sesion.get(url) as respuesta:
        respuesta.raise_for_status()
        hechos = 0
        with open(destino, "wb") as archivo:
            async for trozo in respuesta.content.iter_chunked(1 << 18):
                archivo.write(trozo)
                hechos += len(trozo)
                if al_avanzar:
                    al_avanzar(hechos)


class Descargas:
    """Descargas en segundo plano, una por voz a la vez, con su progreso y su error."""

    def __init__(self):
        self._tareas: dict[str, asyncio.Task] = {}
        self._avance: dict[str, int] = {}
        self._error: dict[str, str] = {}

    def activa(self, voz_id: str) -> bool:
        return voz_id in self._tareas

    def progreso(self, voz_id: str) -> int | None:
        return self._avance.get(voz_id)

    def error(self, voz_id: str) -> str | None:
        return self._error.get(voz_id)

    def iniciar(self, voz_id: str) -> None:
        """Lanza la descarga y vuelve enseguida; ErrorDeVoz si no se puede."""
        voz = CATALOGO.get(voz_id)
        if voz is None or not voz.es_piper:
            raise ErrorDeVoz("esa voz no se descarga")
        if self.activa(voz_id):
            raise ErrorDeVoz("ya se está descargando")
        if instalada(voz_id):
            raise ErrorDeVoz("ya está instalada")
        self._error.pop(voz_id, None)
        self._avance[voz_id] = 0
        self._tareas[voz_id] = asyncio.create_task(self._correr(voz))

    async def _correr(self, voz: Voz) -> None:
        # La config va primero y el modelo al final: la voz cuenta como instalada con ambos.
        archivos = [
            (voz.url_config, ruta_config(voz.id), voz.md5_config, None),
            (voz.url_modelo, ruta_modelo(voz.id), voz.md5_modelo, self._avanzar(voz)),
        ]
        temporales = [(destino.with_name(destino.name + ".part"), destino) for _, destino, _, _ in archivos]
        try:
            config.CARPETA_DE_VOCES.mkdir(parents=True, exist_ok=True)
            for (url, _, md5, avance), (temporal, _) in zip(archivos, temporales):
                await _bajar(url, temporal, avance)
                if await asyncio.to_thread(_md5, temporal) != md5:
                    raise ErrorDeVoz("lo bajado no coincide con el md5 del catálogo")
            for temporal, destino in temporales:
                os.replace(temporal, destino)
            self._avance[voz.id] = 100
            log.info("Voz %s instalada", voz.id)
        except Exception as error:
            log.warning("No pude bajar la voz %s: %s", voz.id, error)
            self._error[voz.id] = str(error) or type(error).__name__
        finally:
            for temporal, _ in temporales:
                _borrar_temporal(temporal)
            self._tareas.pop(voz.id, None)

    def _avanzar(self, voz: Voz):
        def avance(hechos: int) -> None:
            self._avance[voz.id] = min(99, hechos * 100 // voz.tamano)    # el 100 es de la voz ya instalada
        return avance


descargas = Descargas()


def borrar(voz_id: str) -> None:
    """Borra una voz instalada que no esté en uso ni bajándose."""
    voz = CATALOGO.get(voz_id)
    if voz is None or not voz.es_piper:
        raise ErrorDeVoz("esa voz no se borra")
    if voz_id == ajustes.obtener("voz"):
        raise ErrorDeVoz("esa voz está en uso: elige otra antes de borrarla")
    if descargas.activa(voz_id):
        raise ErrorDeVoz("se está descargando")
    for ruta in (ruta_modelo(voz_id), ruta_config(voz_id)):
        ruta.unlink(missing_ok=True)


def _guardar(**cambios) -> None:
    try:
        ajustes.guardar(**cambios)
    except OSError as error:
        raise ErrorDeVoz(f"no pude guardar el ajuste: {error}") from error


def elegir(voz_id: str) -> None:
    """Fija la voz del DJ; solo una instalada y, si es de Piper, con Piper disponible."""
    if not instalada(voz_id):
        raise ErrorDeVoz("esa voz no está instalada")
    if CATALOGO[voz_id].es_piper and not piper_disponible():
        raise ErrorDeVoz("falta piper-tts: instálalo con pip en el venv del bot")
    _guardar(voz=voz_id)


def poner_filtro(activo: bool) -> None:
    _guardar(filtro_radio=activo)


def listar() -> list[dict]:
    """El catálogo con el estado de cada voz, listo para el panel."""
    en_uso = ajustes.obtener("voz")
    return [{
        "id": v.id, "pais": v.pais, "nombre": v.nombre, "calidad": v.calidad, "tamano": v.tamano,
        "instalada": instalada(v.id), "descargando": descargas.activa(v.id),
        "progreso": descargas.progreso(v.id), "error": descargas.error(v.id), "en_uso": v.id == en_uso,
    } for v in CATALOGO.values()]
