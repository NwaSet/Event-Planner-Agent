import pytest
import requests
import socket


@pytest.fixture(autouse=True)
def simulated_city_ranking(monkeypatch):
    """Classement synthétique pour les scénarios métier ; Overpass est testé séparément."""
    from model.agents import tools

    def rank(file_name, citie, x=5, *, cities=None):
        if citie.startswith("Ville "):
            raise ValueError("Ville de départ introuvable dans Overpass.")
        return sorted(cities, key=lambda city: (city != "Dinant", city))[:x]

    monkeypatch.setattr(tools, "select_filtered", rank)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Les tests ne doivent effectuer aucun appel réseau.")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
