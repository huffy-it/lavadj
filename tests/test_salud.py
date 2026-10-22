"""Cómo viene la transmisión: bien, flojo o mudo.

Una sola pieza de estado con dos salidas —el panel la pinta, el DJ la dice—,
para que no haya dos lógicas de "¿esto está mal?" que se puedan desincronizar.

El caso que motivó todo esto: el bot vivo, contestando 200 en /api/status, con
el autoplay muerto y sin que nada sonara. Desde afuera parecía todo verde.
"""

import pytest

from core.salud import HUECO_MAXIMO, HUECOS_RECORDADOS, Salud


def _reloj():
    """Un reloj que se mueve solo cuando uno lo empuja."""
    marca = {"t": 0.0}

    def ahora():
        return marca["t"]

    def avanzar(segundos):
        marca["t"] += segundos

    return ahora, avanzar


def test_recien_arrancado_esta_bien():
    salud = Salud(reloj=_reloj()[0])
    assert salud.estado(al_aire=False, hay_cola=False)["estado"] == "bien"


def test_dos_fallos_seguidos_lo_dejan_flojo():
    ahora, _ = _reloj()
    salud = Salud(reloj=ahora)

    salud.fallo()
    salud.fallo()

    parte = salud.estado(al_aire=True, hay_cola=True)
    assert parte["estado"] == "flojo"
    assert parte["fallos_seguidos"] == 2


def test_que_suene_un_tema_lo_devuelve_a_bien():
    """Un fallo aislado no es una enfermedad: el de YouTube es intermitente."""
    ahora, _ = _reloj()
    salud = Salud(reloj=ahora)

    salud.fallo()
    salud.fallo()
    salud.sono()

    assert salud.estado(al_aire=True, hay_cola=True)["estado"] == "bien"


def test_al_aire_con_cola_y_sin_sonar_nada_esta_mudo():
    """El caso de los logs: vivo, con cola, y el autoplay muerto hace rato."""
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    salud.sono()

    avanzar(600)

    parte = salud.estado(al_aire=True, hay_cola=True)
    assert parte["estado"] == "mudo"
    assert parte["motivo"]


def test_sin_cola_el_silencio_no_es_estar_mudo():
    """Nadie pidió nada: que no suene música es lo correcto, no una falla."""
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    salud.sono()

    avanzar(600)

    assert salud.estado(al_aire=True, hay_cola=False)["estado"] == "bien"


def test_fuera_del_aire_tampoco_esta_mudo():
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)

    avanzar(600)

    assert salud.estado(al_aire=False, hay_cola=True)["estado"] == "bien"


def test_estar_mudo_le_gana_a_estar_flojo():
    """Se informa lo más grave: quedarse sin música tapa cualquier otra cosa."""
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    salud.sono()
    salud.fallo()
    salud.fallo()

    avanzar(600)

    assert salud.estado(al_aire=True, hay_cola=True)["estado"] == "mudo"


# --- cuándo hablar y cuándo callarse -----------------------------------------

def test_el_primer_tema_que_se_cae_se_avisa():
    salud = Salud(reloj=_reloj()[0])
    assert "Gato por liebre" in salud.rendido("Gato por liebre")


def test_el_motivo_se_dice_si_se_sabe():
    salud = Salud(reloj=_reloj()[0])
    frase = salud.rendido("Gato por liebre", "está bloqueada")
    assert "está bloqueada" in frase


def test_los_temas_siguientes_no_se_repiten():
    """Si dijera uno por uno, cuatro temas caídos serían cuatro interrupciones."""
    salud = Salud(reloj=_reloj()[0])
    salud.rendido("uno")

    assert salud.rendido("dos") is None
    assert salud.rendido("tres") is None


def test_despues_de_varios_caidos_larga_un_resumen_una_sola_vez():
    salud = Salud(reloj=_reloj()[0])
    frases = [salud.rendido(f"tema {n}") for n in range(1, 9)]

    resumenes = [f for f in frases[1:] if f]
    assert len(resumenes) == 1
    assert "4" in resumenes[0]


def test_si_vuelve_a_sonar_musica_se_reinicia_el_cortafuegos():
    """Zafó: el próximo tema que se caiga vuelve a merecer un aviso."""
    salud = Salud(reloj=_reloj()[0])
    salud.rendido("uno")
    salud.sono()

    assert salud.rendido("dos") is not None


# --- el hueco entre pistas -----------------------------------------------------

def _hueco(salud, avanzar, segundos, guild_id=1):
    """Un final seguido de un arranque `segundos` después; devuelve lo medido."""
    salud.termino(guild_id)
    avanzar(segundos)
    return salud.arranco(guild_id)


def test_el_hueco_es_lo_que_pasa_entre_que_termina_una_pista_y_arranca_la_siguiente():
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)

    assert _hueco(salud, avanzar, 4.5) == pytest.approx(4.5)


def test_arrancar_sin_un_final_pendiente_no_mide_nada():
    """El primer tema de la sesión no viene de ninguna transición."""
    salud = Salud(reloj=_reloj()[0])

    assert salud.arranco(1) is None
    assert salud.estado(al_aire=True, hay_cola=True)["hueco"] is None


def test_cada_final_se_mide_una_sola_vez():
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    _hueco(salud, avanzar, 2)

    avanzar(10)

    assert salud.arranco(1) is None
    assert salud.estado(al_aire=True, hay_cola=True)["hueco"]["muestras"] == 1


def test_si_no_viene_otra_pista_el_final_se_descarta():
    """La cola se vació: lo que suene después es un `!play` nuevo, no una transición."""
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    salud.termino(1)
    salud.sin_siguiente(1)

    avanzar(10)

    assert salud.arranco(1) is None
    assert salud.estado(al_aire=True, hay_cola=True)["hueco"] is None


def test_descartar_un_servidor_sin_final_pendiente_no_revienta():
    Salud(reloj=_reloj()[0]).sin_siguiente(99)


def test_un_hueco_mayor_al_tope_no_cuenta():
    """Pasado el tope ya no es un hueco entre temas, y ensuciaría el promedio."""
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)

    assert _hueco(salud, avanzar, HUECO_MAXIMO + 1) is None
    assert salud.estado(al_aire=True, hay_cola=True)["hueco"] is None


def test_un_hueco_justo_en_el_tope_todavia_cuenta():
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)

    assert _hueco(salud, avanzar, HUECO_MAXIMO) == pytest.approx(HUECO_MAXIMO)


def test_el_hueco_descartado_por_largo_tampoco_deja_final_pendiente():
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    _hueco(salud, avanzar, HUECO_MAXIMO + 1)

    assert salud.arranco(1) is None


def test_dos_servidores_no_se_pisan():
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)

    salud.termino(1)
    avanzar(1)
    salud.termino(2)
    avanzar(2)

    assert salud.arranco(2) == pytest.approx(2)
    assert salud.arranco(1) == pytest.approx(3)


def test_descartar_el_final_de_un_servidor_no_toca_el_de_otro():
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    salud.termino(1)
    salud.termino(2)

    salud.sin_siguiente(1)
    avanzar(5)

    assert salud.arranco(1) is None
    assert salud.arranco(2) == pytest.approx(5)


def test_las_estadisticas_del_hueco_resumen_las_muestras():
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    for segundos in (1.0, 5.0, 2.0):
        _hueco(salud, avanzar, segundos)

    assert salud.estado(al_aire=True, hay_cola=True)["hueco"] == {
        "ultimo": 2.0,
        "promedio": 2.67,       # 8 / 3, redondeado a dos decimales
        "maximo": 5.0,
        "muestras": 3,
    }


def test_solo_se_recuerdan_los_ultimos_huecos():
    """Si no, el promedio quedaría anclado a lo que pasó hace horas."""
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    medidos = [float(n) for n in range(1, HUECOS_RECORDADOS + 6)]
    for segundos in medidos:
        _hueco(salud, avanzar, segundos)

    recordados = medidos[-HUECOS_RECORDADOS:]
    hueco = salud.estado(al_aire=True, hay_cola=True)["hueco"]
    assert hueco["muestras"] == HUECOS_RECORDADOS
    assert hueco["promedio"] == pytest.approx(sum(recordados) / len(recordados), abs=0.01)
    assert hueco["ultimo"] == medidos[-1]


def test_el_hueco_viaja_en_cualquier_estado():
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    _hueco(salud, avanzar, 3)
    salud.fallo()
    salud.fallo()
    assert salud.estado(al_aire=True, hay_cola=True)["estado"] == "flojo"
    assert salud.estado(al_aire=True, hay_cola=True)["hueco"]["ultimo"] == 3.0

    avanzar(600)
    parte = salud.estado(al_aire=True, hay_cola=True)
    assert parte["estado"] == "mudo"
    assert parte["hueco"]["ultimo"] == 3.0


def test_medir_un_hueco_no_cuenta_como_que_sono_musica():
    """`arranco` también se llama con los clips del DJ; `sono()` es otra cosa."""
    ahora, avanzar = _reloj()
    salud = Salud(reloj=ahora)
    salud.fallo()
    salud.fallo()
    avanzar(100)

    _hueco(salud, avanzar, 1)

    parte = salud.estado(al_aire=True, hay_cola=True)
    assert parte["estado"] == "flojo"
    assert parte["fallos_seguidos"] == 2
    assert parte["sin_sonar"] == 101
