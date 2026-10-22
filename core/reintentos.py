"""Qué hacer cuando Lavalink no puede sacar el audio de una pista.

Función pura: decide y no actúa. El cog aplica la decisión (mensajes, cola, contadores).
"""

from dataclasses import dataclass
from enum import Enum

# YouTube falla de a ratos: el mismo video que se cae ahora suele sonar al segundo intento.
REINTENTOS_POR_PISTA = 2

# Temas perdidos seguidos antes de dar la transmisión por caída: sin tope, con el
# autoplay encendido se pasaría el día probando temas.
CAIDAS_SEGUIDAS_MAX = 3

CORTE_MINIMO_MS = 5000      # un corte más temprano se reintenta desde el principio
FINAL_MS = 15000            # y uno en los últimos segundos, también
RESPIRO_MS = 2000           # el reintento retrocede esto para no perder el empalme


class Accion(Enum):
    SALTAR = "saltar"           # fallo sin arreglo: no gasta reintentos
    RENDIRSE = "rendirse"       # se agotaron los reintentos de este tema
    PARAR = "parar"             # demasiados temas perdidos seguidos
    REINTENTAR = "reintentar"


@dataclass(frozen=True)
class Decision:
    accion: Accion
    caidas: int                     # caídas seguidas del servidor después de decidir
    reanudar_en: int | None = None  # ms desde donde seguir; solo en REINTENTAR


def decidir(
    motivo: str | None, gastados: int, caidas: int, corte_ms: int, largo_ms: int
) -> Decision:
    """`motivo` es el fallo definitivo (o None); `corte_ms`, por dónde iba el tema (0 si no aplica)."""
    if motivo:
        return Decision(Accion.SALTAR, caidas)
    if gastados >= REINTENTOS_POR_PISTA:
        caidas += 1
        return Decision(Accion.PARAR if caidas >= CAIDAS_SEGUIDAS_MAX else Accion.RENDIRSE, caidas)
    reanudar = None
    if CORTE_MINIMO_MS < corte_ms and (not largo_ms or corte_ms < largo_ms - FINAL_MS):
        reanudar = corte_ms - RESPIRO_MS
    return Decision(Accion.REINTENTAR, caidas, reanudar)
