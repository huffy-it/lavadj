"""La política de reintentos: una función pura, sin player ni Discord."""

import dataclasses

import pytest

from core.reintentos import CAIDAS_SEGUIDAS_MAX as MAX, REINTENTOS_POR_PISTA as REINTENTOS, Accion, Decision, decidir

LARGO = 180_000


def _decidir(motivo=None, gastados=0, caidas=0, corte=0, largo=LARGO):
    return decidir(motivo, gastados, caidas, corte, largo)


@pytest.mark.parametrize("kwargs", [{}, {"gastados": 0}, {"gastados": REINTENTOS}, {"corte": 60_000}])
def test_un_motivo_definitivo_saltea_sin_tocar_las_caidas_ni_reanudar(kwargs):
    assert _decidir("está bloqueada", caidas=2, **kwargs) == Decision(Accion.SALTAR, 2)


@pytest.mark.parametrize("gastados, caidas", [(g, 1) for g in range(REINTENTOS)] + [(0, MAX)])
def test_mientras_queden_reintentos_se_reintenta_desde_el_principio_sin_sumar_caidas(gastados, caidas):
    assert _decidir(gastados=gastados, caidas=caidas) == Decision(Accion.REINTENTAR, caidas, None)


@pytest.mark.parametrize("corte, largo, esperado", [
    (60_000, LARGO, 58_000),
    (5_000, LARGO, None),
    (5_001, LARGO, 3_001),
    (LARGO - 15_000, LARGO, None),
    (LARGO - 15_001, LARGO, LARGO - 15_001 - 2_000),
    (LARGO, LARGO, None),
    (170_000, 0, 168_000),
    (5_000, 0, None),
])
def test_donde_se_reanuda_un_reintento(corte, largo, esperado):
    assert _decidir(corte=corte, largo=largo).reanudar_en == esperado


@pytest.mark.parametrize("gastados, caidas, esperado", [
    (REINTENTOS, 0, Decision(Accion.RENDIRSE, 1)),
    (REINTENTOS + 5, 0, Decision(Accion.RENDIRSE, 1)),
    (REINTENTOS, MAX - 2, Decision(Accion.RENDIRSE, MAX - 1)),
    (REINTENTOS, MAX - 1, Decision(Accion.PARAR, MAX)),
    (REINTENTOS, MAX + 2, Decision(Accion.PARAR, MAX + 3)),
])
def test_agotados_los_reintentos_se_rinde_o_para_y_nunca_reanuda(gastados, caidas, esperado):
    assert _decidir(gastados=gastados, caidas=caidas, corte=60_000) == esperado


def test_la_decision_es_inmutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _decidir().accion = Accion.PARAR
