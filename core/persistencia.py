"""La cola de cada servidor en disco, para retomarla si el bot se reinicia.

Una foto por servidor, todas en un solo JSON. Nada aquí importa wavelink ni discord:
las pistas viajan como su `raw_data`, que es lo que hace falta para rearmarlas.
"""

import contextlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from core import config

log = logging.getLogger("persistencia")

VERSION = 1

# Pasado esto nadie espera la cola: retomarla pondría música de otra tarde a quien entró después.
VIGENCIA = 6 * 3600

# Campo de la foto -> tipo que tiene que traer al cargarla.
CAMPOS = {
    "voz": int, "texto": (int, type(None)), "actual": (dict, type(None)), "posicion": int,
    "cola": list, "modo": str, "volumen": int, "pausado": bool, "autoplay": bool, "hora": (int, float),
}

PEDIDO_POR = "pedido_por"

# Lo que cambia solo con el tiempo: no cuenta para saber si la foto cambió.
_QUE_CORRE = ("hora", "posicion")


def _datos(pista) -> dict | None:
    """El `raw_data` de la pista (más quién la pidió, que no viaja en él); None si no cabe en JSON."""
    datos = pista.raw_data
    pedido_por = getattr(pista, "requester", None)
    if isinstance(pedido_por, str):
        datos = {**datos, PEDIDO_POR: pedido_por}
    try:
        json.dumps(datos)
    except (TypeError, ValueError) as error:
        log.warning("Dejo fuera una pista que no se puede guardar: %s", error)
        return None
    return datos


def tomar(vc, *, posicion: int, autoplay: bool, es_clip, ahora: float | None = None) -> dict | None:
    """La foto del player, o None si no hay nada que retomar.

    `es_clip` dice si una pista es un corte del DJ: esos no se guardan, el wav ya no estará.
    """
    if vc.channel is None:
        return None
    actual = vc.current
    datos = _datos(actual) if actual is not None and not es_clip(actual) else None
    cola = [d for p in vc.queue if not es_clip(p) and (d := _datos(p)) is not None]
    if datos is None and not cola:
        return None
    return {
        "voz": vc.channel.id,
        "texto": getattr(getattr(vc, "text_channel", None), "id", None),
        "actual": datos,
        "posicion": posicion if datos is not None else 0,
        "cola": cola,
        "modo": vc.queue.mode.name,
        "volumen": vc.volume,
        "pausado": bool(vc.paused),
        "autoplay": autoplay,
        "hora": time.time() if ahora is None else ahora,
    }


def huella(fotos: dict[int, dict]) -> str:
    """Lo que identifica el estado de las colas, sin lo que corre solo (hora y posición)."""
    return json.dumps(
        {g: {k: v for k, v in f.items() if k not in _QUE_CORRE} for g, f in fotos.items()},
        sort_keys=True,
    )


def _ruta(ruta) -> Path:
    return Path(ruta or config.ARCHIVO_DE_COLAS)


def _leer(ruta: Path) -> dict:
    """Las fotos crudas del archivo, por ID de servidor en texto. Ausente, corrupto o raro: vacío."""
    try:
        crudo = json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:    # ValueError cubre el JSON roto y los bytes que no son UTF-8
        log.warning("No pude leer %s (%s): lo trato como vacío", ruta, error)
        return {}
    servidores = crudo.get("servidores") if isinstance(crudo, dict) else None
    if not isinstance(servidores, dict) or crudo.get("version") != VERSION:
        log.warning("%s no tiene el formato esperado: lo trato como vacío", ruta)
        return {}
    return servidores


def _escribir(servidores: dict, ruta: Path) -> None:
    """Escribe en un temporal de la misma carpeta y lo cambia de golpe: nunca queda un JSON a medias."""
    texto = json.dumps({"version": VERSION, "servidores": servidores})
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, temporal = tempfile.mkstemp(dir=ruta.parent, prefix=ruta.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as archivo:
            archivo.write(texto)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(temporal, ruta)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporal)
        raise


def guardar(fotos: dict[int, dict], ruta=None) -> None:
    """Deja en el archivo exactamente estas fotos."""
    _escribir({str(g): f for g, f in fotos.items()}, _ruta(ruta))


def _validar(foto) -> None:
    """Levanta ValueError si la foto no trae todos los campos con su tipo."""
    if not isinstance(foto, dict) or foto.keys() != CAMPOS.keys():
        raise ValueError("faltan o sobran campos")
    for campo, tipo in CAMPOS.items():
        if not isinstance(foto[campo], tipo):
            raise ValueError(f"{campo} no es {tipo}")
    if not all(isinstance(p, dict) for p in foto["cola"]):
        raise ValueError("la cola trae algo que no es una pista")


def cargar(ruta=None, ahora: float | None = None) -> dict[int, dict]:
    """Las fotos vigentes por ID de servidor; lo vencido o ilegible se descarta con su aviso en el log."""
    ahora = time.time() if ahora is None else ahora
    fotos = {}
    for clave, foto in _leer(_ruta(ruta)).items():
        try:
            guild_id = int(clave)
            _validar(foto)
        except ValueError as error:
            log.warning("Descarto la foto de %r: %s", clave, error)
            continue
        if ahora - foto["hora"] > VIGENCIA:
            log.info("La foto de %s es de hace %.1f h: no la retomo", guild_id, (ahora - foto["hora"]) / 3600)
            continue
        fotos[guild_id] = foto
    return fotos


def borrar(guild_id: int, ruta=None) -> None:
    """Saca un servidor del archivo sin tocar a los demás; sin archivo o sin él no hace nada."""
    ruta = _ruta(ruta)
    servidores = _leer(ruta)
    if servidores.pop(str(guild_id), None) is None:
        return
    try:
        _escribir(servidores, ruta)
    except OSError:
        log.warning("No pude borrar la foto de %s de %s", guild_id, ruta, exc_info=True)
