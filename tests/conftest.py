import pytest

from app.auth import LIMITER


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    # Cada teste com banco próprio, modo offline, sem rede e segredos conhecidos: nada vaza entre testes.
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LLM_PROVIDER", "offline")
    monkeypatch.setenv("WEB_TOOLS", "off")
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("OPERATOR_TOKEN", "test-operator")
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "1000")
    LIMITER.reset()
    yield
    LIMITER.reset()
