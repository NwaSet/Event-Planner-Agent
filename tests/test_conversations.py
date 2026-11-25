"""Persistance et consultation locales, sans modèles ni vrais fichiers utilisateur."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from controller import event_controller as controller
from dao import conversation_dao as dao


def stored_event():
    return {"request": "Demande originale", "requirements": {"guests": 120, "budget_total": None},
            "planning": {"start": "2026-12-05T22:00", "end": "2026-12-06T04:00"},
            "venue": {"id": "salle_010"}, "catering": {"id": "traiteur_001", "formula_id": "formule_01"},
            "providers": [{"id": "002", "kit_id": 6}], "budget": None,
            "weather": [{"city": "Namur", "date": "2026-12-05", "status": "unavailable", "source": "Open-Meteo"}],
            "documents": [{"content": " \r\nObjet : devis – 1\u202f300 € !\n  ", "approved": False}],
            "plan_approval": {"approved": True, "content": "Plan proposé"},
            "tasks": {"documents": {"status": "refused", "error": "Brouillon refusé"}}}


def test_create_unique_independent_records_and_readable_title():
    request = " \tSoirée à Namur\n" + "a" * 100
    first, second = dao.create_conversation(request), dao.create_conversation(request)
    assert first["conversation_id"] != second["conversation_id"]
    assert len(first["title"]) == 80 and "\n" not in first["title"]
    assert first["messages"] == [{"role": "user", "content": request}]
    assert datetime.fromisoformat(first["created_at"]).utcoffset() is not None
    first["event_data"]["venue"] = {"id": "salle_010"}
    first["messages"].append({"role": "assistant", "content": "Un résultat"})
    assert second["event_data"] == {} and len(second["messages"]) == 1


def test_json_roundtrip_preserves_exact_messages_state_types_and_amounts(tmp_path):
    directory = tmp_path / "not-yet-created"
    conversation = dao.create_conversation(" \tÉvénement à Namur\n")
    response = "\r\n  Brouillon : « Merci ! »\t1\u202f300 €\n"
    conversation["messages"].append({"role": "assistant", "content": response})
    conversation["event_data"] = stored_event()
    conversation["event_details"] = {"providers": [{"id": "002", "nom": "B-Sound Events"}],
                                      "costs": {"known": {"provider_ht": 3000.0}, "known_ttc_subtotal": 0}}
    conversation["status"] = "refused"
    path = dao.save_conversation(conversation, directory)
    loaded = dao.load_conversation(conversation["conversation_id"], directory)
    assert loaded == conversation == json.loads(path.read_text(encoding="utf-8"))
    assert loaded["messages"][-1]["content"] == response
    assert loaded["event_data"]["providers"] == [{"id": "002", "kit_id": 6}]
    assert isinstance(loaded["event_details"]["costs"]["known"]["provider_ht"], float)
    loaded["event_data"]["providers"].clear()
    assert dao.load_conversation(conversation["conversation_id"], directory)["event_data"]["providers"]


def test_listing_sorted_by_creation_with_offsets_and_reports_corruption(tmp_path):
    older, newer = dao.create_conversation("Avant"), dao.create_conversation("Après")
    older["created_at"] = older["updated_at"] = "2020-01-01T12:00:00+02:00"
    newer["created_at"] = newer["updated_at"] = "2020-01-01T11:00:00+00:00"
    dao.save_conversation(newer, tmp_path)
    dao.save_conversation(older, tmp_path)  # date d'écriture distincte de la date de création
    (tmp_path / ("a" * 32 + ".json")).write_text("{broken", encoding="utf-8")
    (tmp_path / "invalid-id.json").write_text("{}", encoding="utf-8")
    result = dao.list_conversations(tmp_path)
    assert [item["title"] for item in result["conversations"]] == ["Après", "Avant"]
    assert all(set(item) == {"conversation_id", "title", "created_at", "updated_at", "status"} for item in result["conversations"])
    assert len(result["errors"]) == 2


def test_missing_directory_is_empty_and_read_only(tmp_path):
    directory = tmp_path / "absent"
    assert dao.list_conversations(directory) == {"conversations": [], "errors": []}
    assert not directory.exists()
    with pytest.raises(dao.ConversationError, match="introuvable"):
        dao.load_conversation("a" * 32, directory)


@pytest.mark.parametrize("identifier", ["../.env", "../../data/venues", "C:\\Windows\\win.ini", "/etc/passwd",
                                         "a" * 32 + "/../x", "", None, "a" * 32 + ":stream"])
def test_invalid_ids_cannot_access_arbitrary_paths(tmp_path, identifier):
    with pytest.raises(dao.ConversationError, match="Identifiant"):
        dao.load_conversation(identifier, tmp_path)


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"schema_version": True}, {"messages": "wrong"},
    {"messages": [{"role": "system", "content": "private"}]}, {"event_data": []},
    {"created_at": "2020-01-01T10:00:00"}, {"status": "unknown"},
])
def test_incompatible_format_is_rejected(tmp_path, change):
    conversation = dao.create_conversation("Test")
    conversation.update(change)
    (tmp_path / (conversation["conversation_id"] + ".json")).write_text(json.dumps(conversation), encoding="utf-8")
    with pytest.raises(dao.ConversationError, match="Format"):
        dao.load_conversation(conversation["conversation_id"], tmp_path)


@pytest.mark.parametrize("value", [Path("not-json"), {1, 2}, float("nan"), float("inf"), {1: "numeric key"}])
def test_serialization_errors_are_explicit_not_default_str(tmp_path, value):
    conversation = dao.create_conversation("Test")
    conversation["event_data"] = {"unsupported": value}
    with pytest.raises(dao.ConversationError, match="Format"):
        dao.save_conversation(conversation, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_mismatched_file_and_record_id_is_rejected(tmp_path):
    conversation = dao.create_conversation("Test")
    dao.save_conversation(conversation, tmp_path)
    path = tmp_path / ("a" * 32 + ".json")
    path.write_text(json.dumps(conversation), encoding="utf-8")
    with pytest.raises(dao.ConversationError, match="ne correspond pas"):
        dao.load_conversation("a" * 32, tmp_path)


@pytest.mark.parametrize("stage", ["fsync", "replace"])
def test_failed_atomic_write_keeps_previous_json_and_cleans_temp(tmp_path, monkeypatch, stage):
    conversation = dao.create_conversation("Original")
    path = dao.save_conversation(conversation, tmp_path)
    before = path.read_bytes()
    conversation["messages"].append({"role": "assistant", "content": "Résultat"})
    monkeypatch.setattr(dao.os, stage, Mock(side_effect=OSError("disk failure")))
    with pytest.raises(dao.ConversationError, match="écrire"):
        dao.save_conversation(conversation, tmp_path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
    assert len(dao.load_conversation(conversation["conversation_id"], tmp_path)["messages"]) == 1


def test_directory_write_error_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(dao.ConversationError, match="écrire"):
        dao.save_conversation(dao.create_conversation("Test"), tmp_path / "new")


@pytest.mark.parametrize("status,refusal,expected", [("success", None, "completed"), ("partial", None, "partial"),
                                                    ("partial", "plan", "refused"), ("partial", "draft", "refused")])
def test_controller_automatically_saves_all_outcomes(tmp_path, status, refusal, expected):
    data = {"request": "Exact request", "tasks": {}, "documents": []}
    if refusal == "plan":
        data["plan_approval"] = {"approved": False, "content": "Plan exact"}
    if refusal == "draft":
        data["documents"] = [{"approved": False, "content": " \nRefus exact\t"}]
    orchestrator = SimpleNamespace(run=Mock(return_value=" \nRésultat exact\t"), event_data=data,
                                   event_details={"costs": {"known_ttc_subtotal": 0}}, last_status=status)
    conversation = controller.run_conversation("Exact request", directory=tmp_path, orchestrator_factory=lambda: orchestrator)
    loaded = dao.load_conversation(conversation["conversation_id"], tmp_path)
    assert loaded == conversation and loaded["status"] == expected
    assert loaded["event_data"] == data and loaded["event_details"] == orchestrator.event_details
    orchestrator.event_data["tasks"]["new"] = {"status": "pending"}
    assert "new" not in conversation["event_data"]["tasks"]
    orchestrator.run.assert_called_once_with("Exact request", event_data={}, messages=[], interactive=True)


@pytest.mark.parametrize("error,status", [(RuntimeError("SECRET provider body"), "error"), (KeyboardInterrupt(), "interrupted")])
def test_controller_keeps_useful_decisions_on_error_or_interruption(tmp_path, error, status):
    orchestrator = SimpleNamespace(run=Mock(side_effect=error), event_data=stored_event(), event_details={})
    conversation = controller.run_conversation("Request", directory=tmp_path, orchestrator_factory=lambda: orchestrator)
    assert conversation["status"] == status
    assert dao.load_conversation(conversation["conversation_id"], tmp_path)["event_data"] == stored_event()
    assert "SECRET" not in json.dumps(conversation)


def test_failed_initial_save_stops_before_llm(tmp_path, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(controller, "save_conversation", Mock(side_effect=dao.ConversationError("disk")))
    with pytest.raises(dao.ConversationError):
        controller.run_conversation("Request", directory=tmp_path, orchestrator_factory=factory)
    factory.assert_not_called()


def test_failed_final_save_exposes_result_in_memory_and_keeps_initial_record(tmp_path, monkeypatch):
    orchestrator = SimpleNamespace(run=Mock(return_value="Résultat utile"), event_data=stored_event(),
                                   event_details={}, last_status="partial")
    save = dao.save_conversation

    def fail_final(conversation, directory):
        if conversation["status"] != "running":
            raise dao.ConversationError("disk full")
        return save(conversation, directory)

    monkeypatch.setattr(controller, "save_conversation", fail_final)
    with pytest.raises(dao.ConversationError) as caught:
        controller.run_conversation("Request", directory=tmp_path, orchestrator_factory=lambda: orchestrator)
    conversation = caught.value.conversation
    assert conversation["messages"][-1]["content"] == "Résultat utile"
    assert conversation["event_data"] == stored_event()
    assert dao.load_conversation(conversation["conversation_id"], tmp_path)["status"] == "running"


def test_history_in_new_process_never_imports_agents_or_llm_or_changes_files(tmp_path):
    conversation = dao.create_conversation("Retrouver cet événement")
    conversation["messages"].append({"role": "assistant", "content": "Résultat déjà calculé"})
    conversation["event_data"] = stored_event()
    conversation["status"] = "partial"
    path = dao.save_conversation(conversation, tmp_path)
    before, timestamp = path.read_bytes(), path.stat().st_mtime_ns
    script = """
import sys
from io import StringIO
from rich.console import Console
import main
choices = iter(['2', '1', '', '3'])
main.trace.console = Console(file=StringIO(), width=160)
main.trace.console.input = lambda *args, **kwargs: next(choices)
assert main.main(directory=sys.argv[1]) == 0
assert 'model.llm' not in sys.modules
assert 'model.event_state' not in sys.modules
assert 'model.event_orchestrator' not in sys.modules
output = main.trace.console.file.getvalue()
assert 'Retrouver cet' in output and '002' in output
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == before and path.stat().st_mtime_ns == timestamp


def test_actual_supervisor_snapshot_keeps_selected_names_and_finances_after_catalogs_are_unavailable(tmp_path, monkeypatch):
    from model import agent, trace
    from model.agents import tools
    from model.event_orchestrator import create_orchestrator

    monkeypatch.setattr(trace, "tracer", Mock())

    def fake(messages, tools=None, agent=None):
        if agent == "Supervisor":
            call = SimpleNamespace(id="choose", function=SimpleNamespace(name="decision_making_agent", arguments=json.dumps({
                "demande": "Prestataire", "category": "provider", "tasks": ["provider"],
                "requirements": {"provider_filters": {"ids": ["002"]}}})))
            return SimpleNamespace(content=None, tool_calls=[call])
        assert agent == "decision making agent"
        return SimpleNamespace(content='{"id":"002","kit_id":6}', tool_calls=None)

    chat = Mock(side_effect=fake)
    monkeypatch.setattr(agent, "chat", chat)
    supervisor = create_orchestrator()
    conversation = controller.run_conversation("Prestataire son", directory=tmp_path, orchestrator_factory=lambda: supervisor)
    assert conversation["status"] == "completed" and chat.call_count == 2
    assert conversation["event_details"]["providers"][0]["nom"] == "B-Sound Events"
    assert conversation["event_details"]["costs"]["known"]["provider_ht"] == 3000
    assert conversation["event_data"]["providers"] == [{"id": "002", "kit_id": 6}]
    monkeypatch.setattr(tools, "_read_catalog", Mock(side_effect=AssertionError("Pas de catalogue à la réouverture")))
    monkeypatch.setattr(tools, "ask_user", Mock(side_effect=AssertionError("Pas de validation à la réouverture")))
    monkeypatch.setattr(agent, "chat", Mock(side_effect=AssertionError("Pas de LLM à la réouverture")))
    assert dao.load_conversation(conversation["conversation_id"], tmp_path) == conversation
