"""Cómo viene la transmisión (bien, flojo o mudo): una sola pieza de estado con dos salidas, el panel y el DJ.

Caso que lo motivó: bot vivo, 200 en /api/status, autoplay muerto y nada sonando.
"""

import pytest

from core.salud import HUECO_MAXIMO, HUECOS_RECORDADOS, Salud


class Reloj:
    """Un reloj que se mueve solo cuando uno lo empuja."""

    t = 0.0

    def __call__(self):
        return self.t

    def avanzar(self, segundos):
        self.t += segundos


@pytest.fixture
def reloj():
    return Reloj()


@pytest.fixture
def salud(reloj):
    return Salud(reloj=reloj)


def _parte(salud, al_aire=True, hay_cola=True):
    return salud.estado(al_aire=al_aire, hay_cola=hay_cola)


def _hueco(salud, reloj, segundos, guild_id=1):
    """Un final seguido de un arranque `segundos` después; devuelve lo medido."""
    salud.termino(guild_id)
    reloj.avanzar(segundos)
    return salud.arranco(guild_id)


def test_recien_arrancado_esta_bien(salud):
    assert _parte(salud, al_aire=False, hay_cola=False)["estado"] == "bien"


def test_dos_fallos_seguidos_lo_dejan_flojo_y_que_suene_un_tema_lo_devuelve_a_bien(salud):
    salud.fallo()
    salud.fallo()
    parte = _parte(salud)
    assert parte["estado"] == "flojo" and parte["fallos_seguidos"] == 2

    salud.sono()
    assert _parte(salud)["estado"] == "bien"


@pytest.mark.parametrize("fallos", [0, 2], ids=["solo_mudo", "mudo_le_gana_a_flojo"])
def test_al_aire_con_cola_y_sin_sonar_nada_esta_mudo(salud, reloj, fallos):
    for _ in range(fallos):
        salud.fallo()
    reloj.avanzar(600)

    parte = _parte(salud)
    assert parte["estado"] == "mudo" and parte["motivo"]


@pytest.mark.parametrize("al_aire, hay_cola", [(True, False), (False, True)])
def test_sin_cola_o_fuera_del_aire_el_silencio_no_es_estar_mudo(salud, reloj, al_aire, hay_cola):
    reloj.avanzar(600)
    assert _parte(salud, al_aire, hay_cola)["estado"] == "bien"


def test_el_primer_tema_que_se_cae_se_avisa_con_su_motivo_si_se_sabe(salud):
    assert "Gato por liebre" in salud.rendido("Gato por liebre")
    frase = Salud().rendido("Gato por liebre", "está bloqueada")
    assert "Gato por liebre" in frase and "está bloqueada" in frase


def test_los_temas_siguientes_se_callan_y_el_resumen_sale_una_sola_vez(salud):
    frases = [salud.rendido(f"tema {n}") for n in range(1, 9)]

    assert frases[0] and frases[1:3] == [None, None]
    resumenes = [f for f in frases[1:] if f]
    assert len(resumenes) == 1 and "4" in resumenes[0]


def test_si_vuelve_a_sonar_musica_se_reinicia_el_cortafuegos(salud):
    salud.rendido("uno")
    salud.sono()
    assert salud.rendido("dos") is not None


@pytest.mark.parametrize("segundos", [4.5, HUECO_MAXIMO], ids=["normal", "justo_en_el_tope"])
def test_el_hueco_es_lo_que_pasa_entre_que_termina_una_pista_y_arranca_la_siguiente(salud, reloj, segundos):
    assert _hueco(salud, reloj, segundos) == pytest.approx(segundos)


def test_sin_un_final_pendiente_no_hay_nada_que_medir_ni_que_descartar(salud):
    assert salud.arranco(1) is None
    assert _parte(salud)["hueco"] is None
    salud.sin_siguiente(99)


def test_cada_final_se_mide_una_sola_vez(salud, reloj):
    _hueco(salud, reloj, 2)
    reloj.avanzar(10)

    assert salud.arranco(1) is None
    assert _parte(salud)["hueco"]["muestras"] == 1


def test_un_hueco_mayor_al_tope_no_cuenta_ni_deja_final_pendiente(salud, reloj):
    assert _hueco(salud, reloj, HUECO_MAXIMO + 1) is None
    assert _parte(salud)["hueco"] is None
    assert salud.arranco(1) is None


def test_si_no_viene_otra_pista_el_final_se_descarta_sin_tocar_el_de_otro_servidor(salud, reloj):
    salud.termino(1)
    salud.termino(2)
    salud.sin_siguiente(1)
    reloj.avanzar(5)

    assert salud.arranco(1) is None
    assert _parte(salud)["hueco"] is None
    assert salud.arranco(2) == pytest.approx(5)


def test_dos_servidores_no_se_pisan(salud, reloj):
    salud.termino(1)
    reloj.avanzar(1)
    salud.termino(2)
    reloj.avanzar(2)

    assert salud.arranco(2) == pytest.approx(2)
    assert salud.arranco(1) == pytest.approx(3)


def test_las_estadisticas_del_hueco_resumen_las_muestras(salud, reloj):
    for segundos in (1.0, 5.0, 2.0):
        _hueco(salud, reloj, segundos)

    assert _parte(salud)["hueco"] == {"ultimo": 2.0, "promedio": 2.67, "maximo": 5.0, "muestras": 3}


def test_solo_se_recuerdan_los_ultimos_huecos(salud, reloj):
    medidos = [float(n) for n in range(1, HUECOS_RECORDADOS + 6)]
    for segundos in medidos:
        _hueco(salud, reloj, segundos)

    recordados = medidos[-HUECOS_RECORDADOS:]
    hueco = _parte(salud)["hueco"]
    assert hueco["muestras"] == HUECOS_RECORDADOS
    assert hueco["promedio"] == pytest.approx(sum(recordados) / len(recordados), abs=0.01)
    assert hueco["ultimo"] == medidos[-1]


def test_el_hueco_viaja_en_cualquier_estado(salud, reloj):
    _hueco(salud, reloj, 3)
    salud.fallo()
    salud.fallo()
    assert _parte(salud)["estado"] == "flojo" and _parte(salud)["hueco"]["ultimo"] == 3.0

    reloj.avanzar(600)
    assert _parte(salud)["estado"] == "mudo" and _parte(salud)["hueco"]["ultimo"] == 3.0


def test_medir_un_hueco_no_cuenta_como_que_sono_musica(salud, reloj):
    """`arranco` también se llama con los clips del DJ; `sono()` es otra cosa."""
    salud.fallo()
    salud.fallo()
    reloj.avanzar(100)

    _hueco(salud, reloj, 1)

    parte = _parte(salud)
    assert parte["estado"] == "flojo" and parte["fallos_seguidos"] == 2 and parte["sin_sonar"] == 101
