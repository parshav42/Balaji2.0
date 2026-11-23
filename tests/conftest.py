import pytest
from balaji import config, storage

@pytest.fixture(autouse=True)
def local_db(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'DB_PATH', tmp_path / 'balaji-test.sqlite3')
    monkeypatch.setattr(config, 'EMBED_MODEL', '')
    storage.init_db()
