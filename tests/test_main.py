from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main as app


@pytest.fixture
def terminal(monkeypatch):
    interface = SimpleNamespace(
        title=Mock(), surrond=Mock(), tracer=Mock(),
        console=SimpleNamespace(input=Mock(return_value="  Un événement à Namur  ")),
    )
    monkeypatch.setattr(app, "trace", interface)
    monkeypatch.setattr(app.llm, "BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(app.llm, "MODELE", "test-model")
    monkeypatch.setattr(app.llm, "verifier_configuration", Mock(return_value=None))
    monkeypatch.setattr(app.llm, "nouvelle_demande", Mock())
    monkeypatch.setattr(app, "create_orchestrator", Mock(return_value=Mock(run=Mock(return_value="Voici le résultat"))))
    return interface


def test_single_request_and_final_display(terminal):
    assert app.main() == 0
    terminal.console.input.assert_called_once()
    app.llm.nouvelle_demande.assert_called_once()
    app.create_orchestrator.assert_called_once()
    app.create_orchestrator.return_value.run.assert_called_once_with("Un événement à Namur")
    terminal.surrond.assert_any_call("Voici le résultat", "Résultat", "green")


def test_empty_request_does_not_call_orchestrator(terminal):
    terminal.console.input.return_value = "   "
    assert app.main() == 0
    app.create_orchestrator.assert_not_called()


@pytest.mark.parametrize("missing", ["BASE_URL", "MODELE", "API_KEY"])
def test_missing_configuration_stops_before_input(terminal, monkeypatch, missing):
    if missing == "API_KEY":
        app.llm.verifier_configuration.return_value = "Clé API manquante"
    else:
        monkeypatch.setattr(app.llm, missing, "")
    assert app.main() == 1
    terminal.console.input.assert_not_called()
    app.create_orchestrator.assert_not_called()


@pytest.mark.parametrize("error,exit_code", [(EOFError(), 0), (KeyboardInterrupt(), 130)])
def test_closed_or_interrupted_input(terminal, error, exit_code):
    terminal.console.input.side_effect = error
    assert app.main() == exit_code
    app.create_orchestrator.assert_not_called()


@pytest.mark.parametrize("error", [app.llm.ErreurLLM("API indisponible"), RuntimeError("échec")])
def test_execution_error_is_displayed(terminal, error):
    app.create_orchestrator.return_value.run.side_effect = error
    assert app.main() == 1
    assert terminal.surrond.call_args.args[2] == "red"
