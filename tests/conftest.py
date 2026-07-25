from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import db  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Banco isolado por teste, com a conexão de thread limpa no fim."""
    db.close_thread_connection()
    path = tmp_path / "test.db"
    db.init(path)
    yield path
    db.close_thread_connection()
