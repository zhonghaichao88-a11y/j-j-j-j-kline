import os
os.environ.setdefault("BROWSER_MODE", "playwright")   # unit tests never start a real browser
import importlib
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'radar.sqlite'))
    monkeypatch.setenv('SEED_DEMO', '1')
    monkeypatch.delenv('APP_PASSWORD', raising=False)
    monkeypatch.delenv('YOUTUBE_API_KEY', raising=False)
    monkeypatch.delenv('X_BEARER_TOKEN', raising=False)
    import app.main as main
    importlib.reload(main)
    monkeypatch.setattr(main, 'browser_available', lambda: True)  # don't depend on Playwright being installed
    with TestClient(main.app) as client:
        yield main, client
    main.engine.dispose()

