import pytest

from core import config


@pytest.fixture(autouse=True)
def archivo_de_colas(tmp_path, monkeypatch):
    """Ningún test escribe en el `estado/` del proyecto: el archivo de colas va a una carpeta temporal."""
    ruta = tmp_path / "estado" / "colas.json"
    monkeypatch.setattr(config, "ARCHIVO_DE_COLAS", ruta)
    return ruta
