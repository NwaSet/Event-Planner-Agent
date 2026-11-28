"""État et échanges compacts : vrais agents ReAct, réponses LLM simulées uniquement."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from controller.event_controller import run_conversation, continue_conversation
from dao.conversation_dao import create_conversation, save_conversation, load_conversation
from model import agent as agent_module, trace
from model.agents import create_all_agents, tools
from model.event_orchestrator import Supervisor, CONVERSATION_INSTRUCTIONS, create_orchestrator
from model.event_state import EventRun, encode


BASELINE = json.loads((Path(__file__).parent / "fixtures/context_baseline.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def simulated_only(monkeypatch):
    monkeypatch.setattr(trace, "tracer", Mock())
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))
    monkeypatch.setattr(tools, "ask_user", Mock(side_effect=AssertionError("Validation non simulée")))


def response(content=None, *calls):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


def call(name, **arguments):
    return SimpleNamespace(id=name, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def choose(messages, **kwargs):
    payload = json.loads(messages[1]["content"])
    candidate = payload["search"]["candidates"][0]
    result = {"id": candidate["id"]}
    if payload["task"] == "provider":
        result["kit_id"] = candidate["kits"][0]["id"]
    if payload["task"] == "catering":
        result["formula_id"] = candidate["formules"][0]["id"]
    return response(encode(result))


def prepared():
    run = EventRun(BASELINE["event_data"]["request"], event_data=BASELINE["event_data"], interactive=True)
    for task in run.event_data["tasks"].values():
        task["status"] = "success"
    return run


def test_state_has_three_business_sections_and_separate_workflow():
    run = prepared()
    assert set(run.event_data) == {"requirements", "results", "tasks", "workflow"}
    assert run.event_data["results"]["venue"] == {"id": "salle_054"}
    assert run.event_data["results"]["providers"] == [{"id": "005", "kit_id": 1}]
    assert "selection_filters" not in encode(run.event_data)
    assert not any(key.endswith("_filters") for key in run.event_data["requirements"])
    assert all("result" not in outcome and "task" not in outcome for outcome in run.event_data["tasks"].values())


def test_legacy_requirements_drop_technical_fields_but_keep_mandatory_constraints():
    state = copy.deepcopy(BASELINE["event_data"])
    state["requirements"].update(venue_filters={"ids": ["salle_054"], "accessible": True, "limit": 50},
                                 provider_filters={"max_budget": 2500}, prompt="RAW_PROMPT", raw_result={"large": [1]})
    run = EventRun("Suite", event_data=state, interactive=True)
    req = run.event_data["requirements"]
    assert req["venue_constraints"] == {"ids": ["salle_054"]}
    assert req["provider_constraints"] == {"max_budget": 2500}
    assert req["budget_total"] == 10000
    assert "prompt" not in req and "raw_result" not in req
    assert state["requirements"]["venue_filters"]["limit"] == 50  # migration sans mutation de la source


@pytest.mark.parametrize("task", ["planning", "budget", "venue", "catering", "provider", "documents"])
def test_specialist_projections_never_include_archives_or_other_agent_messages(task):
    run = prepared()
    marker = "RAW_OTHER_AGENT_RESPONSE"
    run.event_data["workflow"]["request"] = marker
    run.event_data["workflow"]["document_archive"] = [{"content": marker, "approved": True}]
    run.event_data["results"]["documents"] = [{"content": marker, "approved": True}]
    run.event_data["workflow"]["plan_approval"] = {"approved": True, "content": marker, "plan": {"large": marker}}
    context = run.get_decision_context(task) if task in ("venue", "catering", "provider") else run.context(task)
    assert marker not in encode(context)
    assert not {"request", "current_request", "tasks", "document_archive", "pending_input"} & set(context)
    if task == "planning":
        assert "budget_total" not in encode(context) and "venue_constraints" in context
    if task == "budget":
        assert "city" not in context["requirements"] and "venue" not in context
    if task in ("venue", "catering", "provider"):
        assert "venue" not in context
        assert not any(key.endswith("_filters") for key in context)
        if task != "venue":
            assert context["venue_id"] == "salle_054"
    if task == "documents":
        assert context["venue"]["nom"] == run.selected_row("venue")["nom"]
        assert context["plan_approved"] is True and context["costs"] == run.costs()


def test_supervisor_excludes_full_history_documents_and_catalog_entries():
    run = prepared()
    run.event_data["results"]["documents"] = [{"content": "SECRET_DRAFT", "approved": True}]
    context = run.conversation_context([{"role": "assistant", "content": "RAW_TOOL_OUTPUT"}] * 100)
    assert "RAW_TOOL_OUTPUT" not in encode(context) and "SECRET_DRAFT" not in encode(context)
    assert set(context["decisions"]["venue"]) == {"id", "name"}
    assert "details" not in context["costs"]


def test_compact_delegation_really_runs_specialist_and_reuses_success(monkeypatch):
    run = EventRun("Une salle à Namur pour 100 personnes", interactive=True)
    supervisor = Supervisor()
    supervisor._bind(run)
    tool = supervisor.fonctions["decision_making_agent"]
    chat = Mock(side_effect=choose)
    monkeypatch.setattr(agent_module, "chat", chat)
    assert tool(instruction="Choisir")["code"] == "invalid_arguments"
    assert chat.call_count == 0
    result = tool(instruction="Choisir une salle", category="venue")
    assert result["status"] == "success" and chat.call_count == 1
    assert "details" not in result and set(result["result"]) == {"id"}
    assert tool(instruction="Même choix", category="venue")["status"] == "reused"
    assert run.counters["delegations"] == run.counters["llm_calls"] == 1
    assert chat.call_args.kwargs["agent"] == "decision making agent"


def test_compact_initial_batch_then_revision_persists_ids_and_full_messages(tmp_path, monkeypatch):
    stages = []
    def fake(messages, tools=None, agent=None):
        stages.append(agent)
        if agent == "Supervisor":
            return response(None, call("revise_event", requirements={"guests": 100}, tasks=["venue"]),
                            call("decision_making_agent", instruction="Salle à Namur", category="venue"))
        return choose(messages)
    monkeypatch.setattr(agent_module, "chat", fake)
    original = run_conversation("Salle à Namur pour 100 personnes", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert original["status"] == "completed" and stages == ["Supervisor", "decision making agent"]
    assert original["event_details"] == {} and len(original["messages"]) == 2
    original_id = original["event_data"]["results"]["venue"]["id"]
    def revise(messages, tools=None, agent=None):
        stages.append(agent)
        if agent == "Supervisor":
            return response(None, call("revise_event", requirements={"city": "Dinant"}),
                            call("decision_making_agent", instruction="Nouvelle ville", category="venue"))
        return choose(messages)
    monkeypatch.setattr(agent_module, "chat", revise)
    # tasks annonce la portée du tour afin de terminer sans appel de conclusion supplémentaire.
    saved = load_conversation(original["conversation_id"], tmp_path)
    updated = continue_conversation(saved, "Change la ville pour Dinant", directory=tmp_path,
                                    orchestrator_factory=lambda: Supervisor(max_iterations=1))
    assert updated["event_data"]["results"]["venue"]["id"] != original_id
    assert updated["messages"][:2] == original["messages"] and len(updated["messages"]) == 4
    assert stages == ["Supervisor", "decision making agent", "Supervisor", "decision making agent"]
    assert updated["event_details"] == {} and load_conversation(updated["conversation_id"], tmp_path) == updated


def test_no_match_stays_terminal_after_reopening_until_relevant_revision(monkeypatch):
    run = EventRun("Salle", interactive=True)
    run.revise_event(requirements={"venue_constraints": {"ids": ["inconnue"]}})
    _, _, _, specialist = create_all_agents()
    assert run.delegate(specialist, "Choisir", "venue")["status"] == "no_match"
    resumed = EventRun("Réessaie", event_data=run.event_data, interactive=True)
    assert resumed.delegate(specialist, "Reformulation", "venue")["status"] == "reused"
    assert resumed.counters["llm_calls"] == 0
    resumed.revise_event(requirements={"venue_constraints": {"ids": ["salle_054"]}})
    assert resumed.event_data["tasks"]["venue"]["status"] == "invalidated"
    monkeypatch.setattr(agent_module, "chat", choose)
    assert resumed.delegate(specialist, "Nouveau choix", "venue")["status"] == "success"
    assert resumed.counters["llm_calls"] == resumed.counters["delegations"] == 1


def test_temporary_search_filters_never_become_persistent_requirements(monkeypatch):
    run = EventRun("Salle à Namur pour 100 personnes", interactive=True)
    before = copy.deepcopy(run.event_data["requirements"])
    monkeypatch.setattr(agent_module, "chat", choose)
    _, _, _, specialist = create_all_agents()
    assert run.delegate(specialist, "Choisir", "venue", requirements={
        "venue_filters": {"max_budget": 100000, "limit": 2}})["status"] == "success"
    assert run.event_data["requirements"] == before
    assert "max_budget" not in encode(run.event_data)


def test_targeted_revision_keeps_independent_results_and_financial_calculations():
    run = prepared()
    previous = copy.deepcopy(run.event_data["results"])
    costs = run.costs()
    run.revise_event(requirements={"provider_constraints": {"max_budget": 1}})
    assert run.event_data["requirements"]["budget_total"] == 10000
    assert run.event_data["tasks"]["provider"]["status"] == "invalidated"
    assert run.event_data["results"]["providers"] == []
    for key in ("planning", "budget", "venue"):
        assert run.event_data["results"][key] == previous[key]
    assert run.costs()["known"]["venue_ttc"] == costs["known"]["venue_ttc"]
    assert run.costs()["deposit"] == costs["deposit"]


@pytest.mark.parametrize("approved", [True, False])
def test_email_approval_exact_text_is_restored_without_any_llm(tmp_path, monkeypatch, approved):
    text = " \r\nObjet : Café — 1\u202f300 € !\t\n  Merci.  "
    run = prepared()
    run.event_data["workflow"]["pending_input"] = {"kind": "email_approval", "content": text,
                                                   "revision": run.event_data["workflow"]["revision"]}
    conversation = create_conversation("Email")
    conversation["event_data"] = run.event_data
    save_conversation(conversation, tmp_path)
    supervisor = Supervisor()
    updated = continue_conversation(load_conversation(conversation["conversation_id"], tmp_path),
                                    "oui" if approved else "non", directory=tmp_path,
                                    orchestrator_factory=lambda: supervisor)
    assert updated["event_data"]["results"]["documents"][0]["content"] == text
    assert updated["event_data"]["results"]["documents"][0]["approved"] is approved
    assert supervisor.counters["llm_calls"] == supervisor.counters["delegations"] == 0
    assert updated["status"] == ("completed" if approved else "refused")


def test_plan_refusal_and_material_change_keep_exact_archives_without_writer(monkeypatch):
    run = prepared()
    run.remember({"document_kind": "email"})
    _, writer, _, _ = create_all_agents()
    assert run.delegate(writer, "Devis", "documents")["status"] == "waiting"
    content = run.event_data["workflow"]["pending_input"]["content"]
    assert run.resolve_pending("non")
    assert run.event_data["workflow"]["plan_approval"]["content"] == content
    assert run.event_data["results"]["documents"] == []
    assert run.counters["llm_calls"] == 0
    run.revise_event(requirements={"guests": 101})
    assert run.event_data["workflow"]["plan_approval"] is None
    assert run.event_data["workflow"]["approval_archive"][0]["content"] == content


def test_old_conversation_read_is_unchanged_and_migration_preserves_approval(tmp_path):
    legacy = copy.deepcopy(BASELINE["event_data"])
    plan = {key: value for key, value in legacy.items() if key not in ("plan_approval", "documents")}
    legacy["plan_approval"] = {"approved": True, "content": "  Plan approuvé.\n ", "plan": plan}
    conversation = create_conversation("Événement")
    conversation["event_data"] = legacy
    path = save_conversation(conversation, tmp_path)
    original_bytes = path.read_bytes()
    restored = load_conversation(conversation["conversation_id"], tmp_path)
    assert restored == conversation and path.read_bytes() == original_bytes
    run = EventRun("Suite", event_data=restored["event_data"], interactive=True)
    assert run.approve_plan() is True
    assert run.event_data["workflow"]["plan_approval"]["content"] == "  Plan approuvé.\n "
    assert path.read_bytes() == original_bytes


def test_context_bytes_are_reduced_against_captured_baseline():
    run = EventRun(BASELINE["event_data"]["request"], event_data=BASELINE["event_data"], interactive=True)
    supervisor = Supervisor()
    supervisor._bind(run)
    values = {"supervisor": {"instructions": supervisor.instructions + CONVERSATION_INSTRUCTIONS,
                              "tools": supervisor.schemas, "context": run.conversation_context([])}}
    values.update({task: run.get_decision_context(task) if task in ("venue", "provider") else run.context(task)
                   for task in ("planning", "budget", "venue", "provider", "documents")})
    for task, value in values.items():
        size = len(encode(value).encode("utf-8"))
        assert size < BASELINE["measurements"]["before"][task]["utf8_bytes"]
    assert len(encode(values["supervisor"]).encode("utf-8")) < .7 * BASELINE["measurements"]["before"]["supervisor"]["utf8_bytes"]
    assert all("requirements" not in schema["function"]["parameters"]["properties"]
               for schema in supervisor.schemas if schema["function"]["name"].endswith("_agent"))
