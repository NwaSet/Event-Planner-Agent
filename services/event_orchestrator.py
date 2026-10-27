"""The supervisor coordinates; specialists decide; Python saves their results."""

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock
from typing import Literal

from agents import (create_budget_agent, create_provider_agent, create_schedule_agent,
                    create_venue_agent, create_writer_agent)
from dao import ConversationDAO, MemoryDAO, TraceDAO
from dao.json_storage import StorageError, json_copy, sanitize_payload, validate_identifier
from model import llm, trace
from model.agent import Agent
from tools.data_tools import get_item


_RUN_LOCK = Lock()
_INSTRUCTIONS = """
You are Orchestrator. Answer in the user's language. Coordinate specialists; do
not re-evaluate their domain decisions or repeat their searches. Use update_event
to merge new/changed user requirements BEFORE delegating. Typical event fields:
type, city, radius_km, attendance, budget, meal, indoor_only, date, preferences.
Preserve explicit false values. meal=false excludes catering; drinks are separate.
The central state contains selections by category, budget, schedule and task status.
Python saves specialist decisions automatically. Never copy their results into an
update tool. IDs are sufficient; specialists resolve details with get_item.

Delegate only needed work via ask_venue_agent, ask_provider_agent (one category:
technical or catering), ask_budget_agent, ask_schedule_agent, ask_writer_agent.
No bar/artists catalogue exists: report that limitation, do not substitute a meal.
Results are reused while inputs are unchanged. Use refresh_reason only for an
explicit user request to replace/reconsider a result; never retry a completed task
without a reason. Stale results remain visible but need the affected specialist.
For a summary, use stored results/WriterAgent without recalling search agents.
Never claim no selection exists when its IDs are in state. Ask only necessary
questions. The first venue/provider candidate is preferred; others are alternatives.

Finish with a normal, useful text response, including available partial results
and limitations. No submit_result tool is needed. Announcing a tool call in prose
does not execute it. Never claim unexecuted work succeeded. Never send emails,
book, purchase, invent availability or mix estimated HT/TTC prices. Drafts only.
""".strip()


@dataclass
class OrchestratorResult:
    conversation_id: str | None
    response: str = ""
    status: Literal["completed", "needs_input", "partial", "error"] = "error"
    required_information: list[str] = field(default_factory=list)
    planning_context: dict = field(default_factory=dict)
    partial_results: dict[str, str] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    persisted: bool = False
    request_id: str | None = None
    usage: dict = field(default_factory=dict)
    request_ids: list[str] = field(default_factory=list)
    delegations: dict[str, int] = field(default_factory=dict)


def _json_response(text: str):
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json_copy(json.loads(text))


def _selection(category: str, result: dict) -> dict:
    """Validate references only, never reconsider capacity, prices or suitability."""
    if not isinstance(result, dict) or result.get("status") not in {"selected", "not_found", "error"}:
        raise ValueError("Selection must have status selected, not_found or error.")
    items = result.get("items")
    if items is None and isinstance(result.get("ids"), list):
        items = [{"id": identifier} for identifier in result["ids"]]
    if items is None:
        items = []
    if not isinstance(items, list) or (result["status"] == "selected" and not items):
        raise ValueError("A selected result requires an item list.")
    if result["status"] != "selected" and items:
        raise ValueError("Only selected results may contain items.")
    selected = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("Each item needs an exact string ID.")
        get_item(category, item["id"], item.get("option_id"))
        reference = {"id": item["id"], "option_id": item.get("option_id")}
        if reference not in selected:
            selected.append(reference)
    return {"status": result["status"], "items": selected}


def _load_state(conversation: dict) -> dict:
    state = {"event": {}, "selections": {}, "budget": None, "schedule": None, "summary": None, "tasks": {}}
    if "event_state" in conversation:
        state.update(json_copy(conversation["event_state"]))
        if not all(isinstance(state[key], dict) for key in ("event", "selections", "tasks")):
            raise ValueError("Invalid saved event state.")
        return state
    # Read old snapshots without modifying their histories or interpreting prose.
    memories = conversation.get("agent_memories", {})
    for message in reversed(memories.get("Orchestrator", [])):
        if message.get("role") == "tool" and message.get("name") == "update_planning_context":
            try:
                old = _json_response(message["content"])
                if not old.get("accepted"):
                    continue
                old = old["planning_context"]
                state["event"] = old.get("requirements", {}) | {"preferences": old.get("preferences", {})}
                state["budget"], state["schedule"] = old.get("budget"), old.get("schedule")
                if state["budget"] and "total_budget" in state["budget"]:
                    state["event"].setdefault("budget", state["budget"]["total_budget"])
                if old.get("venue"):
                    state["selections"]["venue"] = _selection("venue", {"status": "selected", "items": [old["venue"]]})["items"]
                break
            except (ValueError, TypeError, AttributeError, KeyError, LookupError):
                continue
    for agent, category in (("VenueAgent", "venue"), ("ProviderAgent", "technical")):
        if category in state["selections"]:
            continue
        for message in reversed(memories.get(agent, [])):
            if message.get("role") != "assistant" or message.get("tool_calls"):
                continue
            try:
                old = _json_response(message.get("content") or "")
                items = old.get("items", old.get("selections"))
                if not items:
                    continue
                state["selections"][category] = _selection(category, {"status": "selected", "items": items})["items"]
                break
            except (ValueError, TypeError, AttributeError, LookupError):
                continue
    return state


@dataclass
class _Turn:
    conversation_id: str
    state: dict
    results: dict = field(default_factory=dict)
    failures: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)


class EventOrchestrator:
    def __init__(self, directory: str | Path | None = None, *, max_iterations: int = 12,
                 max_delegations: int = 8, max_calls_per_agent: int = 2):
        if any(type(n) is not int or n <= 0 for n in (max_iterations, max_delegations, max_calls_per_agent)):
            raise ValueError("Execution limits must be positive integers.")
        self.conversation_dao, self.memory_dao, self.trace_dao = ConversationDAO(directory), MemoryDAO(directory), TraceDAO(directory)
        self.max_delegations, self.max_calls_per_agent = max_delegations, max_calls_per_agent
        self.venue_agent, self.provider_agent = create_venue_agent(), create_provider_agent()
        self.budget_agent, self.schedule_agent, self.writer_agent = create_budget_agent(), create_schedule_agent(), create_writer_agent()
        self._turn: _Turn | None = None
        specialists = {"venue": self.venue_agent, "provider": self.provider_agent, "budget": self.budget_agent,
                       "schedule": self.schedule_agent, "writer": self.writer_agent}
        tools = [self._delegation(role, agent) for role, agent in specialists.items()]
        tools.append((self._update_event, llm.tool_schema("update_event", "Merge user requirements only; selections are saved automatically.",
                     {"changes": {"type": "object"}}, required=["changes"])))
        self.supervisor = Agent("Orchestrator", _INSTRUCTIONS, tools, max_iterations=max_iterations)
        self.agents = (self.supervisor, *specialists.values())

    def _inputs(self, role: str, category: str, request: str = "") -> dict:
        state = self._turn.state
        data = {"event": state["event"]}
        if role == "provider":
            data.update(category=category, selections={"venue": state["selections"].get("venue", [])})
        elif role in {"budget", "schedule", "writer"}:
            data["selections"] = state["selections"]
            upstream = {"budget": {"venue", "provider"}, "schedule": {"venue", "provider", "budget"},
                        "writer": {"venue", "provider", "budget", "schedule"}}[role]
            data["pending_results"] = {key: {"status": task["status"], "stale": task.get("stale", False)}
                                       for key, task in state["tasks"].items() if task["role"] in upstream
                                       and (task.get("stale") or task["status"] not in {"selected", "completed"})}
            if role in {"schedule", "writer"}:
                data["budget"] = state["budget"]
            if role == "writer":
                data.update(schedule=state["schedule"])
        return data

    def _signature(self, role, category, request=""):
        data = (self._inputs(role, category), request if role == "writer" else "")
        return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _save_state(self):
        # Mark dependent results stale without deleting useful earlier work.
        for task in self._turn.state["tasks"].values():
            task["stale"] = task["status"] == "error" or task["signature"] != self._signature(task["role"], task["category"], task.get("request", ""))
        self.conversation_dao.save_event_state(self._turn.conversation_id, self._turn.state)

    def _update_event(self, changes: dict) -> dict:
        if self._turn is None or not isinstance(changes, dict):
            raise ValueError("An active turn and event changes object are required.")
        self._turn.state["event"].update(sanitize_payload(json_copy(changes)))
        if changes.get("meal") is False:
            self._turn.state["selections"].pop("catering", None)
        self._save_state()
        return {"event": self._turn.state["event"]}

    def _delegation(self, role: str, agent: Agent):
        def delegate(request: str, category: str = "technical", refresh_reason: str = ""):
            turn = self._turn
            if turn is None or not isinstance(request, str) or not request.strip() or not isinstance(refresh_reason, str):
                raise ValueError("An active turn and specialist request are required.")
            category = category if role == "provider" else role
            key = f"{role}:{category}"
            if role == "provider" and (category not in {"technical", "catering"} or
                                        (category == "catering" and turn.state["event"].get("meal") is False)):
                turn.state["selections"][category] = []
                turn.state["tasks"][key] = {"role": role, "category": category,
                                           "signature": self._signature(role, category), "status": "not_found"}
                self._save_state()
                return {"status": "not_found", "items": [], "reason": "No catalogue for this service, or meals explicitly excluded; drinks are separate."}
            signature = self._signature(role, category, request)
            saved = turn.state["tasks"].get(key)
            if saved and saved["signature"] == signature and not refresh_reason and saved["status"] != "error":
                result = ({"status": saved["status"], "items": turn.state["selections"].get(category, [])}
                          if role in {"venue", "provider"} else turn.state["summary" if role == "writer" else role])
                return {"result": result, "reused": True}
            if sum(turn.counts.values()) >= self.max_delegations or turn.counts.get(agent.name, 0) >= self.max_calls_per_agent:
                turn.failures[key] = f"Delegation limit reached for {agent.name}."
                return {"error": turn.failures[key]}
            turn.counts[agent.name] = turn.counts.get(agent.name, 0) + 1
            tool_names = None
            if role == "provider":
                excluded = {"search_caterers", "get_caterer"} if category == "technical" else {"search_providers", "get_provider"}
                tool_names = [name for name in agent.functions if name not in excluded]
            try:
                inputs = {"task": request, **self._inputs(role, category)}
                text = agent.run(json.dumps(inputs, ensure_ascii=False),
                                 conversation_id=turn.conversation_id, tool_names=tool_names)
                turn.results[agent.name] = text
                if agent.last_status != "completed":
                    raise ValueError("Specialist response is incomplete; its earlier saved results remain available.")
                result = text if role == "writer" else _json_response(text)
                if role in {"venue", "provider"}:
                    result = _selection(category, result)
                    if result["status"] == "error":
                        raise ValueError("The specialist reported an error.")
                    turn.state["selections"][category] = result["items"]
                    status = result["status"]
                else:
                    if role != "writer" and not isinstance(result, dict):
                        raise ValueError("Budget/schedule results must be JSON objects.")
                    status = result.get("status", "completed") if isinstance(result, dict) else "completed"
                    if status == "error":
                        raise ValueError("The specialist reported an error.")
                    turn.state["summary" if role == "writer" else role] = result
                turn.state["tasks"][key] = {"role": role, "category": category, "signature": signature,
                                           "status": status, "request": request if role == "writer" else ""}
                self._save_state()
                turn.failures.pop(key, None)
                return result
            except (llm.LLMError, ValueError, LookupError, TypeError, StorageError, OSError):
                turn.state["tasks"][key] = {"role": role, "category": category, "signature": signature,
                                           "status": "error", "stale": True, "request": request if role == "writer" else ""}
                turn.failures[key] = f"{agent.name} returned no usable result or persistence failed; inspect saved work."
                turn.failures[key] += " " + " ".join(agent.last_issues)
                trace.error("Orchestrator", turn.failures[key], turn.conversation_id)
                return {"error": turn.failures[key]}

        properties = {"request": {"type": "string"}, "category": {"type": "string", "default": "technical"},
                      "refresh_reason": {"type": "string", "description": "Only for an explicit user-requested replacement/reconsideration."}}
        return delegate, llm.tool_schema(f"ask_{role}_agent", f"Ask {agent.name}; saved results are reused when inputs match.", properties, required=["request"])

    def get_planning_context(self, conversation_id: str) -> dict:
        conversation = self.conversation_dao.get_conversation(conversation_id)
        if conversation is None:
            raise LookupError("Conversation does not exist.")
        return _load_state(conversation)

    @staticmethod
    def usage_summary(conversation_id: str | None = None, *, request_id: str | None = None) -> dict:
        return llm.usage_summary(conversation_id, request_id=request_id)

    def run(self, request: str, conversation_id: str | None = None) -> OrchestratorResult:
        if not isinstance(request, str) or not request.strip():
            raise ValueError("Request must be a non-empty string.")
        if conversation_id is not None:
            validate_identifier(conversation_id, "Conversation ID")
        if not _RUN_LOCK.acquire(blocking=False):
            raise RuntimeError("Concurrent or recursive orchestrator runs are not supported.")
        result = OrchestratorResult(conversation_id)
        try:
            conversation_id = conversation_id or self.conversation_dao.create_conversation()
            result.conversation_id = conversation_id
            conversation = self.conversation_dao.get_conversation(conversation_id)
            if conversation is None:
                raise LookupError("Conversation does not exist.")
            self._turn = _Turn(conversation_id, _load_state(conversation))
            for agent in self.agents:
                agent.load_history(conversation["agent_memories"].get(agent.name, []), conversation_id)
            self.conversation_dao.add_message(conversation_id, "user", request)
            self._save_state()
            try:
                result.response = sanitize_payload(self.supervisor.run(request, conversation_id, planning_context=self._turn.state))
                result.status = self.supervisor.last_status
                result.issues.extend(self.supervisor.last_issues)
            except llm.LLMError:
                result.issues.append("Supervisor could not finish its response; saved work is still available.")
            except BaseException:
                self._persist(result)
                raise
            result.issues.extend(self._turn.failures.values())
            if result.issues and result.status == "completed":
                result.status = "partial"
            saved_work = any(self._turn.state["selections"].values()) or any(
                self._turn.state[key] is not None for key in ("budget", "schedule", "summary"))
            if saved_work and (result.status == "error" or result.response in self.supervisor.last_issues):
                result.status = "partial"
                references = "; ".join(f"{category}: " + ", ".join(item["id"] for item in items)
                                       for category, items in self._turn.state["selections"].items() if items)
                result.response = "Partial results saved — " + (references or "budget, schedule or summary available in event state") + ". The response could not be completed."
            result.planning_context = deepcopy(self._turn.state)
            result.partial_results = sanitize_payload(self._turn.results)
            result.delegations = dict(self._turn.counts)
            result.request_id = self.supervisor.last_request_id
            result.request_ids = [result.request_id] if result.request_id else []
            result.usage = llm.usage_summary(conversation_id, request_ids=result.request_ids)
            result.persisted = self._persist(result)
            if not result.persisted:
                result.status = "error"
            return result
        except (StorageError, OSError, ValueError):
            result.issues.append("Conversation initialization or persistence failed.")
            return result
        finally:
            self._turn = None
            _RUN_LOCK.release()

    def _persist(self, result: OrchestratorResult) -> bool:
        saved = True
        try:
            self._save_state()
            for agent in self.agents:
                self.memory_dao.save_memory(self._turn.conversation_id, agent.name, agent.get_history(self._turn.conversation_id))
            if result.response:
                self.conversation_dao.add_message(self._turn.conversation_id, "assistant", result.response, agent="Orchestrator")
        except (StorageError, OSError, ValueError, LookupError):
            result.issues.append("State, history or response persistence was incomplete.")
            saved = False
        return saved
