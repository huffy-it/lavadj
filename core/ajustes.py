"""Ajustes que se eligen desde los paneles y sobreviven al reinicio, en `estado/ajustes.json`.

Un JSON plano. Lo ausente, ilegible o de otro tipo cae al valor por defecto; las claves
que este módulo no conoce se respetan al reescribir.
"""

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

from core import config

log = logging.getLogger("ajustes")

POR_DEFECTO = {"voz": "espeak", "filtro_radio": True, "guionista": None}   # guionista None: sin elección, manda el .env


def _ruta() -> Path:
    return Path(config.ARCHIVO_DE_AJUSTES)


def _leer() -> dict:
    """Lo que hay en el archivo; ausente, corrupto o raro: vacío."""
    try:
        crudo = json.loads(_ruta().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:    # ValueError cubre el JSON roto y los bytes que no son UTF-8
        log.warning("No pude leer %s (%s): uso los valores por defecto", _ruta(), error)
        return {}
    return crudo if isinstance(crudo, dict) else {}


def _valido(clave: str, valor) -> bool:
    if clave == "guionista":
        from core.guionista import NOMBRES    # tardío: guionista importa este módulo
        return valor is None or valor in NOMBRES
    return clave not in POR_DEFECTO or isinstance(valor, type(POR_DEFECTO[clave]))


def cargar() -> dict:
    """Todos los ajustes, con su valor por defecto donde el archivo no trae uno válido."""
    return {**POR_DEFECTO, **{k: v for k, v in _leer().items() if _valido(k, v)}}


def obtener(clave: str):
    return cargar()[clave]


def guardar(**cambios) -> None:
    """Mezcla estos ajustes con los guardados. ValueError si alguno trae el tipo equivocado."""
    for clave, valor in cambios.items():
        if not _valido(clave, valor):
            raise ValueError(f"{clave} no puede ser {valor!r}")
    ruta = _ruta()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    # Un temporal de la misma carpeta y un cambio de golpe: nunca queda un JSON a medias.
    fd, temporal = tempfile.mkstemp(dir=ruta.parent, prefix=ruta.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as archivo:
            json.dump({**_leer(), **cambios}, archivo, indent=2)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(temporal, ruta)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporal)
        raise
