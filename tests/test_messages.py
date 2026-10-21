from core.messages import split_message


def test_short_text_single_chunk():
    assert split_message("hola") == ["hola"]


def test_exact_limit_not_split():
    text = "a" * 2000
    assert split_message(text) == [text]


def test_long_plain_text_splits_and_reconstructs():
    text = "".join(f"linea {i}\n" for i in range(1000))
    chunks = split_message(text, limit=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    # Texto sin bloques de código: trocear y reunir reconstruye el original.
    assert "".join(chunks) == text


def test_oversized_single_line_hard_split():
    text = "x" * 5000  # una sola línea más larga que el límite
    chunks = split_message(text, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text


def test_code_block_respects_size_limit():
    # Contrato real (docstring): piezas <= limit garantizado; respetar bloques de
    # código es best-effort ("en lo posible"), no una garantía dura.
    body = "".join(f"codigo {i}\n" for i in range(500))
    text = "```\n" + body + "```"
    chunks = split_message(text, limit=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    # El primer trozo conserva la apertura del bloque de código.
    assert chunks[0].startswith("```")
