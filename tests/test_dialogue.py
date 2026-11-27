"""Tours conversationnels persistants ; modèles et validations simulés, réseau interdit."""

import copy
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from controller.event_controller import run_conversation, continue_conversation
from dao.conversation_dao import load_conversation, list_conversations, save_conversation
from model import agent as agent_module, event_state, trace
from model.agents import tools, create_all_agents
from model.event_orchestrator import create_orchestrator
from model.event_state import EventRun


def message(content=None, *calls):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


def call(name, **args):
    return SimpleNamespace(id=name, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def first_choice(messages):
    payload = json.loads(messages[1]["content"])
    row = payload["search"]["candidates"][0]
    result = {"id": row["id"]}
    if payload["task"] == "catering":
        result["formula_id"] = row["formules"][0]["id"]
    if payload["task"] == "provider":
        result["kit_id"] = row["kits"][0]["id"]
    return message(json.dumps(result))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 25)

    monkeypatch.setattr(event_state, "date", Today)
    monkeypatch.setattr(trace, "tracer", Mock())
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))
    monkeypatch.setattr(tools, "ask_user", Mock(side_effect=AssertionError("Aucune saisie bloquante")))


def fake_initial(messages, tools=None, agent=None):
    if agent == "Supervisor":
        return message(None, call("decision_making_agent", demande="Salle", category="venue", tasks=["venue"],
                                  requirements={"guests": 100, "venue_filters": {"ids": ["salle_010"]}}))
    return first_choice(messages)


def test_multiple_messages_add_catering_and_keep_venue_in_one_file(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "chat", fake_initial)
    conversation = run_conversation("Salle à Namur", directory=tmp_path, orchestrator_factory=create_orchestrator)
    saved_venue = copy.deepcopy(conversation["event_data"]["results"]["venue"])
    calls = []

    def next_turn(messages, tools=None, agent=None):
        calls.append(agent)
        if agent == "Supervisor":
            payload = json.loads(messages[1]["content"])
            assert payload["decisions"]["venue"]["id"] == saved_venue["id"]
            return message(None, call("decision_making_agent", demande="Traiteur", category="catering", tasks=["catering"]))
        return first_choice(messages)

    monkeypatch.setattr(agent_module, "chat", next_turn)
    updated = continue_conversation(conversation, "Ajoute un traiteur", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert updated["conversation_id"] == conversation["conversation_id"]
    assert updated["created_at"] == conversation["created_at"] and updated["title"] == conversation["title"]
    assert len(updated["messages"]) == 4 and len(list(tmp_path.glob("*.json"))) == 1
    assert updated["event_data"]["results"]["venue"] == saved_venue and updated["event_data"]["results"]["catering"]
    assert calls == ["Supervisor", "decision making agent"]
    assert conversation["event_data"]["results"]["catering"] is None


def test_price_question_uses_compact_context_and_no_specialist_with_fresh_limits(monkeypatch):
    supervisor = create_orchestrator(max_llm_calls=2)
    monkeypatch.setattr(agent_module, "chat", fake_initial)
    supervisor.run("Salle à Namur", interactive=True)
    state = copy.deepcopy(supervisor.event_data)
    assert supervisor.counters["llm_calls"] == 2
    history = [{"role": "assistant", "content": "ANCIEN_PROMPT_NE_PAS_TRANSMETTRE"}] * 40
    history += [{"role": "user", "content": "x" * 4000}] * 4

    def price(messages, tools=None, agent=None):
        assert agent == "Supervisor"
        payload = json.loads(messages[1]["content"])
        assert "recent_messages" not in payload
        assert "ANCIEN_PROMPT" not in messages[1]["content"]
        assert payload["costs"]["known"]["venue_ttc"] > 0
        return message("Le prix connu de la salle figure dans les montants enregistrés ; disponibilité à confirmer.")

    monkeypatch.setattr(agent_module, "chat", price)
    response = supervisor.run("Combien coûte la salle ?", event_data=state, messages=history, interactive=True)
    assert response.startswith("Le prix connu") and supervisor.counters["llm_calls"] == 1
    assert supervisor.counters["delegations"] == 0 and supervisor.event_data == state


def test_reopen_legacy_record_and_continue_with_same_id_without_loading_on_open(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "chat", fake_initial)
    conversation = run_conversation("Salle à Namur", directory=tmp_path, orchestrator_factory=create_orchestrator)
    conversation["event_data"].update(conversation["event_data"].pop("workflow"))
    conversation["event_data"].update(conversation["event_data"].pop("results"))
    for key in ("pending_input", "revision", "document_archive", "decision_archive", "approval_archive"):
        conversation["event_data"].pop(key)
    save_conversation(conversation, tmp_path)
    no_call = Mock(side_effect=AssertionError("Pas de LLM à l'ouverture"))
    monkeypatch.setattr(agent_module, "chat", no_call)
    reopened = load_conversation(conversation["conversation_id"], tmp_path)
    assert reopened == conversation
    no_call.assert_not_called()
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message("La salle choisie est conservée.")))
    updated = continue_conversation(reopened, "Quelle salle ?", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert updated["event_data"]["results"]["venue"] == conversation["event_data"]["venue"]
    assert updated["event_data"]["workflow"]["revision"] == 0 and updated["event_data"]["workflow"]["pending_input"] is None
    assert len(updated["messages"]) == 4 and len(list_conversations(tmp_path)["conversations"]) == 1


def test_clarification_is_saved_restored_and_answered_without_losing_original(tmp_path, monkeypatch):
    question = "Dans quelle ville souhaitez-vous l'organiser ?"
    chat = Mock(return_value=message(None, call("clarify", question=question)))
    monkeypatch.setattr(agent_module, "chat", chat)
    conversation = run_conversation("Je veux organiser un événement", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert conversation["status"] == "waiting" and chat.call_count == 1
    assert conversation["messages"][-1]["content"] == question
    reopened = load_conversation(conversation["conversation_id"], tmp_path)
    assert reopened["event_data"]["workflow"]["pending_input"] == {"kind": "clarification", "content": question}

    def answer(messages, tools=None, agent=None):
        if agent == "Supervisor":
            payload = json.loads(messages[1]["content"])
            assert payload["answered_question"] == question
            assert payload["initial_request"] == "Je veux organiser un événement"
            assert payload["current_request"] == "À Namur"
            return message(None, call("decision_making_agent", category="venue", demande="Salle", tasks=["venue"],
                                      requirements={"city": "Namur"}))
        return first_choice(messages)

    monkeypatch.setattr(agent_module, "chat", answer)
    updated = continue_conversation(reopened, "À Namur", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert updated["event_data"]["workflow"]["pending_input"] is None and updated["event_data"]["results"]["venue"]
    assert updated["event_data"]["requirements"]["city"] == "Namur"
    assert updated["event_data"]["workflow"]["plan_approval"] is None


def request_email(conversation, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message(None, call(
        "writer_agent", demande="Devis à la salle", tasks=["documents"], requirements={"document_kind": "email"}))))
    return continue_conversation(conversation, "Rédige un email à la salle", directory=tmp_path, orchestrator_factory=create_orchestrator)


@pytest.mark.parametrize("approved", [True, False])
def test_email_after_venue_uses_two_persisted_validations_and_keeps_exact_text(tmp_path, monkeypatch, approved):
    monkeypatch.setattr(agent_module, "chat", fake_initial)
    conversation = run_conversation("Salle à Namur", directory=tmp_path, orchestrator_factory=create_orchestrator)
    conversation = request_email(conversation, tmp_path, monkeypatch)
    assert conversation["event_data"]["workflow"]["pending_input"]["kind"] == "plan_approval"
    assert not conversation["event_data"]["results"]["documents"]
    agent_module.chat.assert_called_once()  # Supervisor uniquement, pas encore Writer
    draft = " \r\nObjet : Devis – 1\u202f300 € !\n\tMerci.  "

    def writer(messages, tools=None, agent=None):
        assert agent == "writer agent"
        payload = json.loads(messages[1]["content"])
        assert payload["venue"]["id"] == conversation["event_data"]["results"]["venue"]["id"]
        return message(None, call("ask_user", message=draft))

    chat = Mock(side_effect=writer)
    monkeypatch.setattr(agent_module, "chat", chat)
    conversation = continue_conversation(load_conversation(conversation["conversation_id"], tmp_path), "oui",
                                         directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert conversation["status"] == "waiting" and chat.call_count == 1
    assert conversation["event_data"]["workflow"]["pending_input"]["kind"] == "email_approval"
    assert conversation["event_data"]["workflow"]["plan_approval"]["approved"] is True
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("Pas de LLM après validation")))
    # La validation du brouillon doit fonctionner même si la clé API est absente.
    from model import llm
    monkeypatch.setattr(llm, "API_KEY", "")
    monkeypatch.setattr(llm, "BASE_URL", "https://example.test")
    conversation = continue_conversation(load_conversation(conversation["conversation_id"], tmp_path),
                                         "oui" if approved else "non", directory=tmp_path)
    assert conversation["event_data"]["results"]["documents"] == [{"content": draft, "approved": approved,
        "revision": conversation["event_data"]["workflow"]["revision"], "kind": "email", "recipient": "venue",
        "purpose": "quote", "request": "Rédige un email à la salle", "status": "approved" if approved else "refused"}]
    assert conversation["event_data"]["workflow"]["pending_input"] is None
    assert conversation["status"] == ("completed" if approved else "refused")
    agent_module.chat.assert_not_called()
    tools.ask_user.assert_not_called()


def test_refused_plan_never_calls_writer_and_does_not_destroy_venue(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "chat", fake_initial)
    conversation = run_conversation("Salle à Namur", directory=tmp_path, orchestrator_factory=create_orchestrator)
    conversation = request_email(conversation, tmp_path, monkeypatch)
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("Plan refusé")))
    conversation = continue_conversation(conversation, "non", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert conversation["status"] == "refused" and conversation["event_data"]["results"]["venue"]
    assert conversation["event_data"]["results"]["documents"] == []
    assert conversation["event_data"]["workflow"]["plan_approval"]["approved"] is False
    agent_module.chat.assert_not_called()


PLANNING = {"start": "2026-12-19T20:00", "end": "2026-12-20T00:00"}


def prepared_event(monkeypatch):
    run = EventRun("Événement", interactive=True)
    run.remember({"guests": 100, "planning": PLANNING, "venue_filters": {"ids": ["salle_010"]}},
                 ["venue", "catering", "provider"])
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", lambda messages, **kwargs: first_choice(messages))
    for category in ("venue", "catering", "provider"):
        assert run.delegate(decision, category, category)["status"] == "success"
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("Contrôles Python uniquement")))
    return run


@pytest.mark.parametrize("guests,venue_kept", [(110, True), (150, False)])
def test_guest_change_rechecks_capacity_and_cost_preserving_independent_decisions(monkeypatch, guests, venue_kept):
    run = prepared_event(monkeypatch)
    before = copy.deepcopy(run.event_data)
    old_cost = run.costs()["known"]["catering_ttc"]
    result = run.revise_event(requirements={"guests": guests})
    assert result["status"] == "updated" and bool(run.event_data["results"]["venue"]) == venue_kept
    assert run.event_data["requirements"]["guests"] == guests
    assert run.event_data["results"]["planning"] == before["results"]["planning"]
    assert run.event_data["results"]["providers"] == before["results"]["providers"]
    assert run.event_data["results"]["catering"] == before["results"]["catering"]
    assert run.costs()["known"]["catering_ttc"] > old_cost
    if not venue_kept:
        assert run.event_data["tasks"]["venue"]["status"] == "invalidated"
        assert any(item["result"] == before["results"]["venue"] for item in run.event_data["workflow"]["decision_archive"])
    agent_module.chat.assert_not_called()


def test_budget_change_calls_budgetary_then_invalidates_only_over_limit_offers(monkeypatch):
    run = prepared_event(monkeypatch)
    before = copy.deepcopy(run.event_data)
    assert run.revise_event(requirements={"budget_total": 4000})["status"] == "updated"
    assert run.event_data["tasks"]["budget"]["status"] == "invalidated"
    budget, _, _, _ = create_all_agents()
    chat = Mock(return_value=message(json.dumps({"venue": .3, "catering": .5, "provider": .2})))
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(budget, "Répartis 4000 euros", "budget")["status"] == "success"
    assert chat.call_count == 1
    assert run.event_data["results"]["venue"] == before["results"]["venue"]
    assert run.event_data["results"]["providers"] == before["results"]["providers"]
    assert run.event_data["results"]["planning"] == before["results"]["planning"]
    assert run.event_data["results"]["catering"] is None
    assert run.event_data["tasks"]["catering"]["status"] == "invalidated"
    assert run.costs()["allocations"] == {"venue": 1200, "catering": 2000, "provider": 800}


def test_venue_replacement_rechecks_imposed_provider_and_keeps_date(monkeypatch):
    run = prepared_event(monkeypatch)
    old_provider = copy.deepcopy(run.event_data["results"]["providers"])
    assert old_provider[0]["id"] in ("005", "013")
    run.revise_event(replace=["venue"], remove=["catering"])
    assert run.event_data["results"]["providers"] == old_provider
    old_venue = next(row for row in run.catalogs["venue"] if row["id"] == "salle_010")
    assert not run.services_available(old_venue)
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", lambda messages, **kwargs: first_choice(messages))
    result = run.delegate(decision, "Autre salle", "venue", requirements={"venue_filters": {"ids": ["salle_001"]}})
    assert result["status"] == "success"
    assert run.event_data["results"]["venue"]["id"] == "salle_001"
    assert run.event_data["results"]["planning"] == PLANNING
    assert run.event_data["results"]["providers"] == [] and run.event_data["tasks"]["provider"]["status"] == "invalidated"
    assert run.delegate(decision, "Prestataire autorisé par la nouvelle salle", "provider")["status"] == "success"
    assert run.event_data["results"]["providers"][0]["id"] in ("002", "003")
    assert any(item["result"] == old_provider for item in run.event_data["workflow"]["decision_archive"])


def test_remove_catering_keeps_other_decisions_and_drops_its_cost(monkeypatch):
    run = prepared_event(monkeypatch)
    before = copy.deepcopy(run.event_data)
    run.revise_event(remove=["catering"])
    assert run.event_data["results"]["catering"] is None and "catering" not in run.event_data["tasks"]
    assert "catering_ttc" not in run.costs()["known"]
    for field in ("venue", "providers", "planning"):
        assert run.event_data["results"][field] == before["results"][field]
    agent_module.chat.assert_not_called()


def test_next_saturday_passes_previous_schedule_to_planning_and_preserves_selections(monkeypatch):
    run = prepared_event(monkeypatch)
    run.turn_request = "Décale au samedi suivant"
    before = copy.deepcopy(run.event_data)
    run.revise_event(replace=["planning"])
    assert run.event_data["results"]["planning"] is None
    _, _, planning, _ = create_all_agents()
    shifted = {"start": "2026-12-26T20:00", "end": "2026-12-27T00:00"}

    def propose(messages, **kwargs):
        context = json.loads(messages[1]["content"])
        assert context["previous_planning"] == PLANNING
        assert context["instruction"] == "Samedi suivant"
        return message(json.dumps(shifted))

    monkeypatch.setattr(agent_module, "chat", propose)
    assert run.delegate(planning, "Samedi suivant", "planning")["status"] == "success"
    assert run.event_data["results"]["planning"] == shifted
    for field in ("venue", "catering", "providers"):
        assert run.event_data["results"][field] == before["results"][field]


def test_new_schedule_invalidates_venue_with_earlier_music_deadline(monkeypatch):
    run = prepared_event(monkeypatch)
    run.revise_event(replace=["planning"])
    _, _, planning, _ = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message(json.dumps(
        {"start": "2026-12-26T20:00", "end": "2026-12-27T01:00"}))))
    assert run.delegate(planning, "Fin à une heure", "planning")["status"] == "success"
    assert run.event_data["results"]["venue"] is None and run.event_data["tasks"]["venue"]["status"] == "invalidated"


@pytest.mark.parametrize("change", [{"guests": -1}, {"budget_total": -10}, {"date": "hier"}, {"unknown": True}])
def test_invalid_edit_keeps_complete_state(monkeypatch, change):
    run = prepared_event(monkeypatch)
    before = copy.deepcopy(run.event_data)
    with pytest.raises(ValueError):
        run.revise_event(requirements=change)
    assert run.event_data == before


@pytest.mark.parametrize("approved", [True, False, None])
def test_material_edit_archives_exact_document_and_requires_new_plan_approval(monkeypatch, approved):
    run = prepared_event(monkeypatch)
    draft = " \r\nObjet : l'événement — devis !\n\tMerci.  "
    revision = run.event_data["workflow"]["revision"]
    run.event_data["workflow"]["plan_approval"] = {"approved": True, "content": "Ancien plan", "plan": {}, "revision": revision}
    run.event_data["tasks"]["documents"] = {"status": "waiting" if approved is None else "success"}
    run.event_data["requirements"]["document_kind"] = "email"
    if approved is None:
        run.event_data["workflow"]["pending_input"] = {"kind": "email_approval", "content": draft, "revision": revision}
    else:
        run.event_data["results"]["documents"] = [{"content": draft, "approved": approved, "revision": revision}]
    run.revise_event(requirements={"guests": 110})
    assert run.event_data["results"]["documents"] == [] and run.event_data["workflow"]["plan_approval"] is None
    assert run.event_data["workflow"]["pending_input"] is None
    assert run.event_data["workflow"]["document_archive"] == [{"content": draft, "approved": approved, "revision": revision}]
    assert run.event_data["workflow"]["approval_archive"][0]["approved"] is True
    assert run.event_data["workflow"]["revision"] > revision
    assert draft not in run.summary("Modifié")
    assert draft not in json.dumps(run.conversation_context([]))
    _, writer, _, _ = create_all_agents()
    assert run.delegate(writer, "Nouvel email", "documents")["status"] == "waiting"
    assert run.event_data["workflow"]["pending_input"]["kind"] == "plan_approval"
    assert run.event_data["workflow"]["pending_input"]["plan"]["requirements"]["guests"] == 110
    agent_module.chat.assert_not_called()


def test_new_constraint_added_by_delegation_invalidates_old_approval(monkeypatch):
    run = prepared_event(monkeypatch)
    revision = run.event_data["workflow"]["revision"]
    run.event_data["workflow"]["plan_approval"] = {"approved": True, "plan": {}, "content": "Plan", "revision": revision}
    run.event_data["results"]["documents"] = [{"approved": True, "content": "  Ancien texte  ", "revision": revision}]
    run.remember({"rental_period": "soiree"})
    assert run.event_data["workflow"]["plan_approval"] is None and run.event_data["results"]["documents"] == []
    assert run.event_data["workflow"]["document_archive"][0]["content"] == "  Ancien texte  "
    assert run.event_data["workflow"]["document_archive"][0]["revision"] == revision


def test_specialist_search_criteria_survive_resume_and_allow_new_headcount(monkeypatch):
    run = EventRun("Un traiteur pour 100 convives", interactive=True)
    _, _, _, decision = create_all_agents()

    def search_then_select(messages, **kwargs):
        if len(messages) == 2:
            return message(None, call("get_caterers", guests=100, province="Namur"))
        row = json.loads(messages[-1]["content"])["candidates"][0]
        return message(json.dumps({"id": row["id"], "formula_id": row["formules"][0]["id"]}))

    monkeypatch.setattr(agent_module, "chat", search_then_select)
    assert run.delegate(decision, "Traiteur pour 100 convives", "catering", tasks=["catering"])["status"] == "success"
    resumed = EventRun("Finalement 110 convives", interactive=True, event_data=run.event_data)
    before = resumed.costs()["known"]["catering_ttc"]
    resumed.revise_event(requirements={"guests": 110})
    assert resumed.event_data["requirements"]["guests"] == 110
    assert "catering_constraints" not in resumed.event_data["requirements"]
    assert resumed.costs()["known"]["catering_ttc"] > before


def test_supervisor_modifies_saved_constraints_without_recalling_compatible_specialists(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "chat", fake_initial)
    conversation = run_conversation("Salle à Namur", directory=tmp_path, orchestrator_factory=create_orchestrator)
    replies = iter([message(None, call("revise_event", requirements={"guests": 110})), message("Effectif actualisé.")])
    chat = Mock(side_effect=lambda messages, **kwargs: next(replies))
    monkeypatch.setattr(agent_module, "chat", chat)
    updated = continue_conversation(conversation, "Finalement nous serons 110", directory=tmp_path,
                                    orchestrator_factory=create_orchestrator)
    assert updated["event_data"]["requirements"]["guests"] == 110
    assert updated["event_data"]["results"]["venue"] == conversation["event_data"]["results"]["venue"]
    assert all(item.kwargs["agent"] == "Supervisor" for item in chat.call_args_list)
    assert load_conversation(updated["conversation_id"], tmp_path) == updated


def test_two_conversations_stay_independent_and_error_does_not_poison_next_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "chat", fake_initial)
    first = run_conversation("Première salle", directory=tmp_path, orchestrator_factory=create_orchestrator)
    second = run_conversation("Seconde salle", directory=tmp_path, orchestrator_factory=create_orchestrator)
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=RuntimeError("Erreur simulée")))
    failed = continue_conversation(first, "Une question", directory=tmp_path, orchestrator_factory=create_orchestrator)
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message("La salle est toujours enregistrée.")))
    recovered = continue_conversation(failed, "Quelle salle ?", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert recovered["messages"][-1]["content"] == "La salle est toujours enregistrée."
    assert recovered["event_data"]["results"]["venue"] == first["event_data"]["results"]["venue"]
    assert len(recovered["messages"]) == 6
    assert recovered["conversation_id"] != second["conversation_id"]
    assert load_conversation(second["conversation_id"], tmp_path) == second
    assert len(list(tmp_path.glob("*.json"))) == 2
