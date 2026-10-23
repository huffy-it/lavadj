import pytest

from core.messages import split_message


@pytest.mark.parametrize("texto", ["hola", "a" * 2000])
def test_texto_dentro_del_limite_no_se_trocea(texto):
    assert split_message(texto) == [texto]


@pytest.mark.parametrize("texto", ["".join(f"linea {i}\n" for i in range(1000)), "x" * 5000], ids=["lineas", "una_sola_linea"])
def test_texto_largo_se_trocea_sin_pasar_el_limite_y_se_reconstruye(texto):
    chunks = split_message(texto, limit=200)
    assert len(chunks) > 1 and all(len(c) <= 200 for c in chunks)
    assert "".join(chunks) == texto


def test_bloque_de_codigo_largo_respeta_el_limite_y_conserva_la_apertura():
    # El límite es garantía dura; respetar los bloques de código es best-effort.
    text = "```\n" + "".join(f"codigo {i}\n" for i in range(500)) + "```"
    chunks = split_message(text, limit=200)
    assert len(chunks) > 1 and all(len(c) <= 200 for c in chunks)
    assert chunks[0].startswith("```")
