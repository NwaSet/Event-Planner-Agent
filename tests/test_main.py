from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rich.console import Console

import main as app
from controller import event_controller as controller
from dao import conversation_dao as dao


@pytest.fixture
def terminal(monkeypatch, tmp_path):
    output = StringIO()
    console = Console(file=output, width=160, color_system=None)
    console.input = Mock(side_effect=["1", "  Un événement à Namur  ", "Quitter", "3"])
    monkeypatch.setattr(app.trace, "console", console)
    orchestrator = SimpleNamespace(run=Mock(return_value="Voici le résultat"), last_status="success",
                                   event_data={"tasks": {}, "documents": []}, event_details={})
    factory = Mock(return_value=orchestrator)
    monkeypatch.setattr(controller, "_create_orchestrator", factory)
    return SimpleNamespace(console=console, output=output, orchestrator=orchestrator, factory=factory, directory=tmp_path)


def test_new_request_is_saved_and_returns_to_menu(terminal):
    assert app.main(directory=terminal.directory) == 0
    terminal.factory.assert_called_once()
    terminal.orchestrator.run.assert_called_once_with("  Un événement à Namur  ", event_data={}, messages=[], interactive=True)
    assert "Voici le résultat" in terminal.output.getvalue()
    assert "Conversation sauvegardée" in terminal.output.getvalue()
    assert terminal.console.input.call_count == 4
    entry = dao.list_conversations(terminal.directory)["conversations"][0]
    assert dao.load_conversation(entry["conversation_id"], terminal.directory)["messages"][0]["content"] == "  Un événement à Namur  "


def test_empty_request_does_not_call_orchestrator_or_save(terminal):
    terminal.console.input.side_effect = ["1", "  ", "3"]
    assert app.main(directory=terminal.directory) == 0
    terminal.factory.assert_not_called()
    assert not list(terminal.directory.iterdir())


@pytest.mark.parametrize("error,exit_code", [(EOFError(), 0), (KeyboardInterrupt(), 130)])
def test_closed_or_interrupted_input(terminal, error, exit_code):
    terminal.console.input.side_effect = error
    assert app.main(directory=terminal.directory) == exit_code
    terminal.factory.assert_not_called()


def test_execution_error_is_saved_displayed_and_menu_remains_accessible(terminal):
    terminal.orchestrator.run.side_effect = RuntimeError("private provider details")
    assert app.main(directory=terminal.directory) == 1
    assert "RuntimeError" in terminal.output.getvalue() and "private provider details" not in terminal.output.getvalue()
    assert dao.list_conversations(terminal.directory)["conversations"][0]["status"] == "error"


def test_partial_result_has_distinct_exit_code_and_display(terminal):
    terminal.orchestrator.last_status = "partial"
    terminal.orchestrator.run.return_value = "Date trouvée, salle non résolue."
    assert app.main(directory=terminal.directory) == 2
    assert "Partielle" in terminal.output.getvalue() and "salle non résolue" in terminal.output.getvalue()


def test_execution_interruption_is_saved(terminal):
    terminal.orchestrator.run.side_effect = KeyboardInterrupt()
    assert app.main(directory=terminal.directory) == 130
    assert dao.list_conversations(terminal.directory)["conversations"][0]["status"] == "interrupted"


def test_invalid_menu_choices_and_empty_history_do_not_run_agents(terminal):
    terminal.console.input.side_effect = ["bad", "2", "3"]
    assert app.main(directory=terminal.directory) == 0
    terminal.factory.assert_not_called()
    assert "Choix invalide" in terminal.output.getvalue() and "Aucune conversation" in terminal.output.getvalue()


def test_history_reads_messages_and_decisions_without_saving_or_running(terminal, monkeypatch):
    conversation = dao.create_conversation("Ancienne demande")
    conversation["event_data"] = {"providers": [{"id": "002", "kit_id": 6}]}
    conversation["messages"].append({"role": "assistant", "content": "Ancien résultat"})
    conversation["status"] = "partial"
    path = dao.save_conversation(conversation, terminal.directory)
    before = path.read_bytes()
    (terminal.directory / ("a" * 32 + ".json")).write_text("broken", encoding="utf-8")
    monkeypatch.setattr(dao, "save_conversation", Mock(side_effect=AssertionError("Lecture seule")))
    terminal.console.input.side_effect = ["2", "wrong", "9", "1", "", "3"]
    assert app.main(directory=terminal.directory) == 0
    terminal.factory.assert_not_called()
    output = terminal.output.getvalue()
    assert "Conversation illisible" in output
    assert "Ancienne demande" in output and "Ancien résultat" in output and "002" in output
    assert "Historique sauvegardé" in output and path.read_bytes() == before


def test_two_new_conversations_do_not_reuse_previous_state(terminal):
    first = terminal.orchestrator
    first.event_data = {"venue": {"id": "salle_001"}}
    second = SimpleNamespace(run=Mock(return_value="Autre résultat"), last_status="success", event_data={}, event_details={})
    terminal.factory.side_effect = [first, second]
    terminal.console.input.side_effect = ["1", "Première", "Quitter", "1", "Deuxième", "Quitter", "3"]
    assert app.main(directory=terminal.directory) == 0
    entries = dao.list_conversations(terminal.directory)["conversations"]
    assert len(entries) == 2
    assert dao.load_conversation(entries[0]["conversation_id"], terminal.directory)["event_data"] == {}
    assert dao.load_conversation(entries[1]["conversation_id"], terminal.directory)["event_data"] == first.event_data


def test_final_save_failure_shows_useful_result_without_claiming_saved(terminal, monkeypatch):
    save = controller.save_conversation

    def fail_final(conversation, directory):
        if conversation["status"] != "running":
            raise dao.ConversationError("Disque plein")
        return save(conversation, directory)

    monkeypatch.setattr(controller, "save_conversation", fail_final)
    assert app.main(directory=terminal.directory) == 1
    output = terminal.output.getvalue()
    assert "Voici le résultat" in output and "Disque plein" in output
    assert "Conversation sauvegardée" not in output


@pytest.mark.parametrize("missing", ["BASE_URL", "MODELE", "API_KEY"])
def test_missing_configuration_is_saved_as_error_and_does_not_block_history(tmp_path, monkeypatch, missing):
    from model import llm, event_orchestrator

    monkeypatch.setattr(llm, "BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(llm, "MODELE", "test")
    monkeypatch.setattr(llm, "API_KEY", "")
    if missing != "API_KEY":
        monkeypatch.setattr(llm, missing, "")
    factory = Mock(side_effect=AssertionError("Pas de configuration"))
    monkeypatch.setattr(event_orchestrator, "create_orchestrator", factory)
    console = Console(file=StringIO(), width=160)
    console.input = Mock(side_effect=["1", "Demande", "Quitter", "2", "1", "", "3"])
    monkeypatch.setattr(app.trace, "console", console)
    assert app.main(directory=tmp_path) == 0
    factory.assert_not_called()
    assert "Historique sauvegardé" in console.file.getvalue()
    assert dao.list_conversations(tmp_path)["conversations"][0]["status"] == "error"


def test_windows_launcher_opens_one_new_console_and_preserves_exit_code(monkeypatch):
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(app.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    process = Mock(return_value=SimpleNamespace(returncode=2))
    monkeypatch.setattr(app.subprocess, "run", process)
    menu = Mock()
    monkeypatch.setattr(app, "main", menu)
    assert app.launch([]) == 2
    script = Path(app.__file__).resolve()
    process.assert_called_once_with([app.sys.executable, str(script), "--current-console"],
                                    cwd=script.parent, creationflags=16)
    menu.assert_not_called()


@pytest.mark.parametrize("platform,arguments", [("win32", ["--current-console"]), ("linux", [])])
def test_child_and_non_windows_use_rich_menu_without_spawning(monkeypatch, platform, arguments):
    monkeypatch.setattr(app.sys, "platform", platform)
    menu = Mock(return_value=0)
    process = Mock()
    monkeypatch.setattr(app, "main", menu)
    monkeypatch.setattr(app.subprocess, "run", process)
    assert app.launch(arguments) == 0
    menu.assert_called_once_with()
    process.assert_not_called()


def test_new_console_launch_error_is_reported(terminal, monkeypatch):
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(app.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    monkeypatch.setattr(app.subprocess, "run", Mock(side_effect=OSError("failure")))
    assert app.launch([]) == 1
    assert "Impossible d'ouvrir la console Windows" in terminal.output.getvalue()
    terminal.factory.assert_not_called()


def test_terminal_continues_reopens_and_updates_same_file(terminal):
    terminal.console.input.side_effect = ["1", "Salle", "Ajoute un traiteur", "Quitter",
                                         "2", "1", "Combien coûte la salle ?", "Quitter", "3"]
    assert app.main(directory=terminal.directory) == 0
    entries = dao.list_conversations(terminal.directory)["conversations"]
    assert len(entries) == 1 and terminal.factory.call_count == 3
    conversation = dao.load_conversation(entries[0]["conversation_id"], terminal.directory)
    assert [item["content"] for item in conversation["messages"] if item["role"] == "user"] == [
        "Salle", "Ajoute un traiteur", "Combien coûte la salle ?"]
    calls = terminal.orchestrator.run.call_args_list
    assert calls[1].kwargs["event_data"] == calls[2].kwargs["event_data"] == terminal.orchestrator.event_data
    assert len(calls[2].kwargs["messages"]) == 4
