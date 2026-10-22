"""La política de reintentos: una función pura, sin player ni Discord."""

import dataclasses

import pytest

from core.reintentos import (
    CAIDAS_SEGUIDAS_MAX,
    REINTENTOS_POR_PISTA,
    Accion,
    Decision,
    decidir,
)

LARGO = 180_000


def _decidir(motivo=None, gastados=0, caidas=0, corte=0, largo=LARGO):
    return decidir(motivo, gastados, caidas, corte, largo)


# --- fallo sin arreglo ---------------------------------------------------------------

def test_un_motivo_definitivo_saltea_sin_tocar_las_caidas():
    assert _decidir("está bloqueada", caidas=2) == Decision(Accion.SALTAR, 2)


def test_el_motivo_definitivo_manda_aunque_queden_reintentos_o_se_hayan_agotado():
    assert _decidir("está bloqueada", gastados=0).accion is Accion.SALTAR
    assert _decidir("está bloqueada", gastados=REINTENTOS_POR_PISTA).accion is Accion.SALTAR


def test_el_motivo_definitivo_no_reanuda_aunque_haya_un_corte():
    assert _decidir("está bloqueada", corte=60_000).reanudar_en is None


# --- reintentar ----------------------------------------------------------------------

@pytest.mark.parametrize("gastados", range(REINTENTOS_POR_PISTA))
def test_mientras_queden_reintentos_se_reintenta(gastados):
    decision = _decidir(gastados=gastados, caidas=1)
    assert decision.accion is Accion.REINTENTAR
    assert decision.caidas == 1


def test_sin_corte_se_reintenta_desde_el_principio():
    assert _decidir(corte=0) == Decision(Accion.REINTENTAR, 0, None)


def test_a_mitad_de_tema_se_reanuda_menos_un_respiro_de_2_s():
    assert _decidir(corte=60_000).reanudar_en == 58_000


def test_un_corte_de_5_s_o_menos_no_se_reanuda():
    assert _decidir(corte=5_000).reanudar_en is None
    assert _decidir(corte=5_001).reanudar_en == 3_001


def test_un_corte_en_los_ultimos_15_s_no_se_reanuda():
    assert _decidir(corte=LARGO - 15_000).reanudar_en is None
    assert _decidir(corte=LARGO - 15_001).reanudar_en == LARGO - 15_001 - 2_000
    assert _decidir(corte=LARGO).reanudar_en is None


def test_sin_duracion_conocida_se_reanuda_con_cualquier_corte_pasado_de_5_s():
    assert _decidir(corte=170_000, largo=0).reanudar_en == 168_000
    assert _decidir(corte=5_000, largo=0).reanudar_en is None


# --- rendirse y parar ---------------------------------------------------------------

def test_agotados_los_reintentos_se_rinde_y_suma_una_caida():
    assert _decidir(gastados=REINTENTOS_POR_PISTA, caidas=0) == Decision(Accion.RENDIRSE, 1)


def test_con_los_reintentos_de_sobra_tambien_se_rinde():
    assert _decidir(gastados=REINTENTOS_POR_PISTA + 5).accion is Accion.RENDIRSE


def test_rendirse_no_reanuda_aunque_haya_un_corte():
    assert _decidir(gastados=REINTENTOS_POR_PISTA, corte=60_000).reanudar_en is None


def test_la_caida_que_llega_al_tope_para_la_cola():
    antes = CAIDAS_SEGUIDAS_MAX - 1
    assert _decidir(gastados=REINTENTOS_POR_PISTA, caidas=antes) == Decision(
        Accion.PARAR, CAIDAS_SEGUIDAS_MAX
    )


def test_una_caida_antes_del_tope_todavia_solo_se_rinde():
    antes = CAIDAS_SEGUIDAS_MAX - 2
    assert _decidir(gastados=REINTENTOS_POR_PISTA, caidas=antes) == Decision(
        Accion.RENDIRSE, CAIDAS_SEGUIDAS_MAX - 1
    )


def test_pasado_el_tope_sigue_parando():
    decision = _decidir(gastados=REINTENTOS_POR_PISTA, caidas=CAIDAS_SEGUIDAS_MAX + 2)
    assert decision.accion is Accion.PARAR
    assert decision.caidas == CAIDAS_SEGUIDAS_MAX + 3


def test_un_tema_con_reintentos_por_gastar_no_suma_caidas_aunque_ya_haya_muchas():
    decision = _decidir(gastados=0, caidas=CAIDAS_SEGUIDAS_MAX)
    assert decision.accion is Accion.REINTENTAR
    assert decision.caidas == CAIDAS_SEGUIDAS_MAX


# --- la decisión no se puede alterar -------------------------------------------------

def test_la_decision_es_inmutable():
    decision = _decidir()
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.accion = Accion.PARAR
