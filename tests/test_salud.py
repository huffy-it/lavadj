"""Cómo viene la transmisión: bien, flojo o mudo.

Una sola pieza de estado con dos salidas —el panel la pinta, el DJ la dice—,
para que no haya dos lógicas de "¿esto está mal?" que se puedan desincronizar.

El caso que motivó todo esto: el bot vivo, contestando 200 en /api/status, con
el autoplay muerto y sin que nada sonara. Desde afuera parecía todo verde.
"""

from core.salud import Salud


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
