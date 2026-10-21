"""
logging_setup.py - Configura logging a consola + archivo rotativo logs/bot.log.

Se llama una vez al arrancar el bot. Sin esto el bot corre "a ciegas" porque
usa bot.start() (que no configura logging como bot.run()).
"""

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(log_dir: str, level: int = logging.INFO):
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_h = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"),
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_h.setFormatter(fmt)

    console_h = logging.StreamHandler()
    console_h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    # Evitar handlers duplicados si se llama dos veces.
    root.handlers.clear()
    root.addHandler(file_h)
    root.addHandler(console_h)

    # Bajar el ruido de la librería discord (solo warnings hacia arriba).
    logging.getLogger("discord").setLevel(logging.WARNING)
    # El panel consulta /api/status cada 2 s: sin esto el acceso HTTP ocupa casi
    # todo el archivo y la rotación se come en horas el rastro de los fallos.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    logging.getLogger("setup").info("Logging configurado -> %s", os.path.join(log_dir, "bot.log"))
