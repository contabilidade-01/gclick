"""Configuração comum dos testes.

Cada teste roda contra um SQLite temporário (isolado do dados.db real) e com o
pré-aquecimento de cache desligado, para nunca tocar a rede do G-Click.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Garante que o pacote `app` é importável ao rodar `pytest` de qualquer lugar.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, db, helpers, tipos  # noqa: E402


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    """Aponta o banco para um arquivo temporário e desliga o prewarm (rede)."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(helpers, "prewarm", lambda: None)
    db.init()
    tipos.invalidar()  # zera o lru_cache do classificador para ler o banco novo
    yield
    tipos.invalidar()
