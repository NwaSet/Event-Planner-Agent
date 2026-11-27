"""Régression d4d7b578 : demandes Writer après organisation, sans réseau ni LLM réel."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from controller.event_controller import continue_conversation
from dao.conversation_dao import create_conversation, load_conversation, save_conversation
from model import agent as agent_module, trace
from model.event_orchestrator import create_orchestrator
from model.event_state import EventRun


def call(name, **args):
    return SimpleNamespace(id=name, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def response(*calls):
    return SimpleNamespace(content=None, tool_calls=list(calls))


@pytest.fixture
def organized(monkeypatch):
    monkeypatch.setattr(trace, "tracer", Mock())
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))
    run = EventRun("Une soirée à Namur", interactive=True)
    run.event_data["requirements"].update(city="Namur", location_scope="city")
    run.event_data["results"].update(
        venue={"id": "salle_076"}, catering={"id": "traiteur_098", "formula_id": "formule_04"},
        providers=[{"id": "005", "kit_id": 2}],
        planning={"start": "2026-12-11T19:00", "end": "2026-12-11T23:00"})
    run.event_data["tasks"] = {task: {"status": "success"} for task in ("venue", "catering", "provider", "planning")}
    return run.event_data


def test_nested_logged_call_no_longer_discards_email_request(organized):
    run = EventRun("Génère le mail pour le traiteur.", event_data=organized, interactive=True)
    supervisor = create_orchestrator()
    supervisor._bind(run)
    supervisor.fonctions["revise_event"](requirements={"document_kind": "email"}, tasks=["documents"])
    assert run.event_data["tasks"]["documents"]["document"]["recipient"] == "catering"
    assert run.document_requested() and "document_kind" not in run.event_data["requirements"]


def test_logged_signature_and_nested_revision_register_request(organized):
    run = EventRun("Génère le mail pour le traiteur.", event_data=organized, interactive=True)
    supervisor = create_orchestrator()
    supervisor._bind(run)
    calls = json.loads((Path(__file__).parent / "fixtures" / "writer_d4d7b578.json").read_text(encoding="utf-8"))["calls"]
    first = supervisor.fonctions[calls[0]["name"]](**calls[0]["arguments"])
    assert first["status"] in ("updated", "unchanged")
    supervisor.fonctions[calls[1]["name"]](**calls[1]["arguments"])
    assert run.document_requested()
    for logged in calls[3:]:
        outcome = supervisor.fonctions[logged["name"]](**logged["arguments"])
        assert outcome["status"] == "waiting"
    assert run.event_data["workflow"]["pending_input"]["kind"] == "plan_approval"
    assert run.counters["delegations"] == 0


@pytest.mark.parametrize("approved", [True, False])
def test_persisted_two_emails_exact_approval_and_unchanged_event(organized, monkeypatch, tmp_path, approved):
    conversation = create_conversation("Une soirée à Namur")
    conversation["event_data"] = organized
    save_conversation(conversation, tmp_path)
    seen = []
    drafts = [" \r\nObjet : Devis traiteur\n100 personnes. Merci !  ", "Objet : Disponibilité de la salle\nMerci."]

    def simulated(messages, agent=None, **kwargs):
        seen.append(agent)
        if agent == "Supervisor":
            return response(call("writer_agent", instruction="Rédiger le mail demandé."))
        payload = json.loads(messages[1]["content"])
        assert payload["plan_approved"] is True
        assert payload["venue"]["id"] == "salle_076"
        assert payload["planning"] == organized["results"]["planning"]
        target = payload["document"]["recipient"]
        if target == "catering":
            assert payload["catering"]["id"] == "traiteur_098"
            assert payload["catering"]["offer"]["id"] == "formule_04"
            assert "provider" not in payload and payload["document"]["purpose"] == "quote"
            assert "provider" not in payload["costs"]["details"]
            assert "guests" not in payload["requirements"]
        return response(call("ask_user", message=drafts[0 if target == "catering" else 1]))

    monkeypatch.setattr(agent_module, "chat", simulated)
    def turn(text):
        nonlocal conversation
        conversation = continue_conversation(load_conversation(conversation["conversation_id"], tmp_path), text,
                                             directory=tmp_path, orchestrator_factory=create_orchestrator)
        return conversation["event_data"]

    state = turn("Génère le mail pour le traiteur.")
    assert state["workflow"]["pending_input"]["kind"] == "plan_approval" and seen == ["Supervisor"]
    state = turn("oui")
    assert state["workflow"]["pending_input"]["kind"] == "email_approval" and seen[-1] == "writer agent"
    count = len(seen)
    state = turn("oui" if approved else "non")
    assert len(seen) == count
    first = copy.deepcopy(state["results"]["documents"][0])
    assert first["content"] == drafts[0] and first["approved"] is approved and first["recipient"] == "catering"
    state = turn("Génère un email à la salle pour demander sa disponibilité.")
    assert state["workflow"]["pending_input"]["kind"] == "email_approval"
    state = turn("oui")
    docs = state["results"]["documents"]
    assert docs[0] == first and len(docs) == 2
    assert docs[1]["content"] == drafts[1] and docs[1]["approved"] is True and docs[1]["recipient"] == "venue"
    assert conversation["status"] == "completed"
    for key in ("venue", "catering", "providers", "planning", "budget"):
        assert state["results"][key] == organized["results"][key]
    assert state["requirements"] == organized["requirements"]
    assert load_conversation(conversation["conversation_id"], tmp_path) == conversation
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_invitation_and_missing_provider_require_correct_prerequisite(organized, monkeypatch):
    organized["results"]["providers"] = []
    organized["tasks"].pop("provider")
    supervisor = create_orchestrator()
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=response(call("writer_agent", instruction="Rédiger l'email."))))
    supervisor.run("Génère un email au prestataire pour les équipements.", event_data=organized, interactive=True)
    state = supervisor.event_data
    assert state["workflow"]["pending_input"]["kind"] == "clarification"
    assert "Aucun destinataire" in state["workflow"]["pending_input"]["content"]
    assert supervisor.counters["delegations"] == 0 and agent_module.chat.call_count == 1

    supervisor.run("Aux invités", event_data=state, interactive=True)
    state = supervisor.event_data
    assert state["tasks"]["documents"]["document"]["recipient"] == "guests"
    assert state["workflow"]["pending_input"]["kind"] == "plan_approval"

    def writer(messages, agent=None, **kwargs):
        assert agent == "writer agent"
        payload = json.loads(messages[1]["content"])
        assert payload["document"]["recipient"] == "guests"
        assert "provider" not in payload and "catering" not in payload
        assert payload["costs"]["details"] == {}
        return response(call("ask_user", message="Objet : Invitation\nLe 11 décembre 2026, lieu proposé à confirmer."))

    monkeypatch.setattr(agent_module, "chat", writer)
    supervisor.run("oui", event_data=state, interactive=True)
    assert supervisor.event_data["workflow"]["pending_input"]["kind"] == "email_approval"


def test_ambiguous_recipient_and_plan_refusal_can_be_resumed(organized, monkeypatch):
    supervisor = create_orchestrator()
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=response(call("writer_agent", instruction="Mail"))))
    supervisor.run("Génère un email.", event_data=organized, interactive=True)
    state = supervisor.event_data
    assert state["workflow"]["pending_input"]["kind"] == "clarification"
    supervisor.run("Au traiteur, pour un devis", event_data=state, interactive=True)
    assert supervisor.event_data["workflow"]["pending_input"]["kind"] == "plan_approval"
    supervisor.run("non", event_data=supervisor.event_data, interactive=True)
    state = supervisor.event_data
    assert state["workflow"]["plan_approval"]["approved"] is False and state["results"]["documents"] == []
    supervisor.run("Génère un email au traiteur pour demander un devis.", event_data=state, interactive=True)
    assert supervisor.event_data["workflow"]["pending_input"]["kind"] == "plan_approval"
    assert supervisor.event_data["workflow"]["approval_archive"][-1]["approved"] is False


@pytest.mark.parametrize("wording", ["Ne génère pas de mail.", "Organise la soirée sans email."])
def test_pending_task_alone_never_authorizes_unsolicited_writer(organized, wording):
    organized["tasks"]["documents"] = {"status": "pending"}
    run = EventRun(wording, event_data=organized, interactive=True)
    assert not run.document_requested()
