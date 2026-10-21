DISCORD_LIMIT = 2000


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """
    Trocea un texto en piezas <= limit respetando, en lo posible, bloques de
    código y saltos de línea.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    in_code_block = False
    fence = "```"

    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if in_code_block:
                current += fence
                chunks.append(current)
                current = fence + "\n"
            else:
                chunks.append(current)
                current = ""
        current += line
        if line.strip().startswith(fence):
            in_code_block = not in_code_block

    if current:
        chunks.append(current)

    final = []
    for chunk in chunks:
        while len(chunk) > limit:
            final.append(chunk[:limit])
            chunk = chunk[limit:]
        if chunk:
            final.append(chunk)
    return final
