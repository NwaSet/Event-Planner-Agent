import pytest
import requests


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Les tests ne doivent effectuer aucun appel réseau.")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
