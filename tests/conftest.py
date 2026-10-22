import pytest

from core import config


@pytest.fixture(autouse=True)
def archivo_de_colas(tmp_path, monkeypatch):
    """Ningún test escribe en el `estado/` del proyecto: el archivo de colas va a una carpeta temporal."""
    ruta = tmp_path / "estado" / "colas.json"
    monkeypatch.setattr(config, "ARCHIVO_DE_COLAS", ruta)
    return ruta



@pytest.fixture(autouse=True)
def ajustes_y_voces(tmp_path, monkeypatch):
    """Ni los ajustes ni las voces descargadas del proyecto se tocan desde los tests."""
    monkeypatch.setattr(config, "ARCHIVO_DE_AJUSTES", tmp_path / "estado" / "ajustes.json")
    monkeypatch.setattr(config, "CARPETA_DE_VOCES", tmp_path / "voces")
