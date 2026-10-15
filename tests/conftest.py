import pytest


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch):
    """Offline tests must never see real API keys."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield
