"""Offline controller, Rich terminal, and composition-root integration tests."""

from importlib import reload
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rich.console import Console
from rich.prompt import Confirm, Prompt

import main as application
from controller import ControllerError, EventController
from dao import MemoryDAO
from dao import json_storage
from dao.json_storage import StorageIOError
from model import llm, trace
from services import EventOrchestrator, OrchestratorResult
from view import TerminalView


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    output = StringIO()
    console = Console(file=output, width=120, color_system=None, highlight=False)
    monkeypatch.setattr(json_storage, "_LOGS_DIR", directory)
    monkeypatch.setattr(trace, "console", console)
    monkeypatch.setattr(trace, "_error_console", console)
    monkeypatch.setattr(trace, "_listeners", [])
    monkeypatch.setattr(trace, "_console_enabled", False)
    monkeypatch.setattr(trace, "_colors_agents", {})
    monkeypatch.setattr(llm, "USAGE_JOURNAL", [])
    chat = Mock(side_effect=AssertionError("A scripted LLM response is required."))
    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "get_client", Mock(side_effect=AssertionError("No OpenRouter access.")))
    monkeypatch.setattr(llm, "load_dotenv", Mock(side_effect=AssertionError("No credential reads.")))
    return SimpleNamespace(directory=directory, output=output, console=console, chat=chat)


def message(text=None, calls=None):
    return {"role": "assistant", "content": text, "tool_calls": calls or []}


def tool(name, arguments):
    return {"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def finish(text="Proposal ready.", status="completed", questions=None):
    return [message(calls=[tool("submit_result", {"status": status, "response": text,
                                                 "required_information": questions or []})]), message(text)]


def inputs(monkeypatch, values):
    ask = Mock(side_effect=values)
    monkeypatch.setattr(Prompt, "ask", ask)
    return ask


def fake_controller():
    controller = Mock(spec=EventController)
    metadata = {"id": "conv_a", "title": "Birthday", "updated_at": "2026-10-26T14:30:00+00:00", "status": "active"}
    controller.create_conversation.return_value = "conv_a"
    controller.list_conversations.return_value = [metadata]
    controller.get_conversation.return_value = metadata | {"agent_memories": {"BudgetAgent": "PRIVATE_MEMORY"}}
    controller.get_history.return_value = []
    controller.get_usage.return_value = llm.usage_summary("conv_a")
    controller.send_message.return_value = OrchestratorResult("conv_a", response="Ready.", status="completed", persisted=True)
    return controller


def test_controller_reuses_orchestrator_dependencies_without_startup_io(offline):
    orchestrator = EventOrchestrator()
    controller = EventController(orchestrator)
    assert controller.orchestrator is orchestrator
    assert controller.conversation_dao is orchestrator.conversation_dao
    assert not offline.directory.exists()
    offline.chat.assert_not_called()
    llm.get_client.assert_not_called()


def test_controller_conversation_creation_listing_selection_and_management(offline):
    controller = EventController()
    first = controller.create_conversation("First")
    second = controller.create_conversation("Second")
    controller.rename_conversation(first, "Updated")
    controller.archive_conversation(first)
    listed = controller.list_conversations()
    assert listed[0]["id"] == first
    assert {item["id"] for item in listed} == {first, second}
    selected = controller.get_conversation(first)
    assert selected["title"] == "Updated" and selected["status"] == "archived"
    assert "agent_memories" not in selected
    assert controller.get_history(first) == []
    offline.chat.assert_not_called()


def test_controller_sends_and_continues_without_duplicate_persistence(offline, monkeypatch):
    controller = EventController()
    conversation_id = controller.create_conversation("Event")
    add = Mock(wraps=controller.conversation_dao.add_message)
    monkeypatch.setattr(controller.conversation_dao, "add_message", add)
    offline.chat.side_effect = [*finish("First response."), *finish("Second response.")]
    first = controller.send_message(conversation_id, "First request.")
    second = controller.send_message(conversation_id, "Second request.")
    assert first.conversation_id == second.conversation_id == conversation_id
    assert second.status == "completed" and second.persisted
    assert add.call_count == 4
    assert [item["content"] for item in controller.get_history(conversation_id)] == [
        "First request.", "First response.", "Second request.", "Second response.",
    ]
    assert len(controller.list_conversations()) == 1
    assert {entry.kwargs["conversation_id"] for entry in offline.chat.call_args_list} == {conversation_id}


def test_restart_restores_supervisor_and_specialist_memories_through_controller(offline):
    controller = EventController()
    conversation_id = controller.create_conversation()
    delegate = message(calls=[tool("ask_budget_agent", {"request": "Review the budget."})])
    offline.chat.side_effect = [delegate, message("Budget allowance recorded."), *finish()]
    controller.send_message(conversation_id, "Remember a budget of 5000 EUR.")
    saved_supervisor = controller.orchestrator.supervisor.get_history(conversation_id)
    saved_budget = controller.orchestrator.budget_agent.get_history(conversation_id)
    restored = EventController(EventOrchestrator())
    assert restored.get_conversation(conversation_id)["id"] == conversation_id
    offline.chat.reset_mock()
    offline.chat.side_effect = [delegate, message("Previous allowance retained."), *finish("Revised proposal.")]
    result = restored.send_message(conversation_id, "Can we reduce costs?")
    assert result.status == "completed"
    assert offline.chat.call_args_list[0].args[0][:-1] == saved_supervisor
    assert offline.chat.call_args_list[1].args[0][:-1] == saved_budget


def test_delete_requires_confirmation_and_retains_other_conversations_and_traces(offline):
    controller = EventController()
    first, second = controller.create_conversation("Delete me"), controller.create_conversation("Keep me")
    MemoryDAO().save_memory(first, "BudgetAgent", [{"role": "user", "content": "Private budget."}])
    controller.orchestrator.trace_dao.save_trace(trace.info("Orchestrator", "Existing trace", first))
    for confirmed in (False, None, 1, "yes"):
        with pytest.raises(ControllerError) as error:
            controller.delete_conversation(first, confirmed=confirmed)
        assert error.value.code == "confirmation_required"
    controller.delete_conversation(first, confirmed=True)
    assert controller.get_conversation(first) is None
    assert controller.get_conversation(second)
    assert len(controller.orchestrator.trace_dao.get_traces(first)) == 1
    offline.chat.assert_not_called()


@pytest.mark.parametrize("identifier", [None, "", "../invalid", "conv_missing"])
def test_unknown_or_invalid_conversation_never_triggers_implicit_creation(identifier, offline):
    controller = EventController()
    with pytest.raises(ControllerError):
        controller.send_message(identifier, "Hello.")
    assert controller.list_conversations() == []
    offline.chat.assert_not_called()


@pytest.mark.parametrize("user_text", ["", "   ", None, 5])
def test_controller_rejects_empty_or_invalid_messages_before_execution(user_text, offline):
    with pytest.raises(ControllerError, match="non-empty"):
        EventController().send_message("conv_a", user_text)
    offline.chat.assert_not_called()
    assert not offline.directory.exists()


@pytest.mark.parametrize("operation", ["get_history", "rename_conversation", "archive_conversation", "delete_conversation", "get_usage"])
def test_controller_unknown_conversation_operations_have_safe_errors(operation):
    controller = EventController()
    args = ["conv_missing", "Title"] if operation == "rename_conversation" else ["conv_missing"]
    kwargs = {"confirmed": True} if operation == "delete_conversation" else {}
    with pytest.raises(ControllerError) as error:
        getattr(controller, operation)(*args, **kwargs)
    assert error.value.code == "not_found"
    assert controller.get_conversation("conv_missing") is None


@pytest.mark.parametrize("exception,code", [
    (StorageIOError("private storage path"), "storage"),
    (OSError("private path"), "storage"),
    (llm.LLMError("Authorization: Bearer PRIVATE_KEY"), "llm"),
    (ValueError("private invalid data"), "invalid_input"),
    (RuntimeError("private execution detail"), "execution"),
    (Exception("private HTTP request"), "unexpected"),
])
def test_controller_maps_operational_errors_without_sensitive_exception_text(exception, code, monkeypatch):
    controller = EventController()
    conversation_id = controller.create_conversation()
    monkeypatch.setattr(controller.orchestrator, "run", Mock(side_effect=exception))
    with pytest.raises(ControllerError) as error:
        controller.send_message(conversation_id, "Help.")
    assert error.value.code == code
    assert "private" not in str(error.value).lower() and "PRIVATE_KEY" not in str(error.value)


def test_controller_returns_structured_failure_without_saving_it_again(monkeypatch):
    controller = EventController()
    conversation_id = controller.create_conversation()
    failure = OrchestratorResult(conversation_id, status="error", issues=["Supervisor execution failed."], persisted=False)
    monkeypatch.setattr(controller.orchestrator, "run", Mock(return_value=failure))
    assert controller.send_message(conversation_id, "Help.") is failure
    assert controller.get_history(conversation_id) == []


def test_controller_checks_response_id_and_reuses_usage_api(monkeypatch):
    controller = EventController()
    identifier = controller.create_conversation()
    usage = llm.usage_summary(identifier)
    summary = Mock(return_value=usage)
    monkeypatch.setattr(controller.orchestrator, "usage_summary", summary)
    assert controller.get_usage(identifier) == usage
    summary.assert_called_once_with(identifier)
    monkeypatch.setattr(controller.orchestrator, "run", Mock(return_value=OrchestratorResult("conv_other")))
    with pytest.raises(ControllerError) as error:
        controller.send_message(identifier, "Help.")
    assert error.value.code == "integration"


def test_archived_conversation_can_continue_without_losing_archive_status(offline):
    controller = EventController()
    identifier = controller.create_conversation()
    controller.archive_conversation(identifier)
    offline.chat.side_effect = finish()
    assert controller.send_message(identifier, "Continue.").status == "completed"
    assert controller.get_conversation(identifier)["status"] == "archived"


def test_terminal_new_conversation_blank_input_and_human_clarification(monkeypatch, offline):
    controller = fake_controller()
    controller.send_message.side_effect = [
        OrchestratorResult("conv_a", "Quelle date souhaitez-vous ?", "needs_input", ["Date de l'événement ?"], persisted=True),
        OrchestratorResult("conv_a", "La date est enregistrée.", "completed", persisted=True),
    ]
    inputs(monkeypatch, ["1", "Birthday", "  ", "Organise une fête.", "Le 30 novembre.", "/back", "6"])
    TerminalView(controller, offline.console).run()
    controller.create_conversation.assert_called_once_with("Birthday")
    assert [entry.args for entry in controller.send_message.call_args_list] == [
        ("conv_a", "Organise une fête."), ("conv_a", "Le 30 novembre."),
    ]
    output = offline.output.getvalue()
    assert "Information needed" in output and "Quelle date" in output
    assert "La date est enregistrée." in output and "PRIVATE_MEMORY" not in output
    assert output.count("Goodbye.") == 1


def test_terminal_supported_commands_are_local_and_history_is_visible_only(monkeypatch, offline):
    controller = fake_controller()
    controller.get_history.return_value = [
        {"role": "user", "content": "User message"}, {"role": "assistant", "content": "Visible response"},
        {"role": "tool", "content": "PRIVATE_TOOL"}, {"role": "system", "content": "PRIVATE_SYSTEM"},
    ]
    inputs(monkeypatch, ["2", "conv_a", "/help", "/history", "/usage", "/back", "6"])
    TerminalView(controller, offline.console).run()
    controller.send_message.assert_not_called()
    controller.get_usage.assert_called_once_with("conv_a")
    assert controller.get_history.call_count == 2
    output = offline.output.getvalue()
    assert "User message" in output and "Visible response" in output
    assert "PRIVATE_" not in output
    for command in ("/back", "/history", "/usage", "/help", "/exit"):
        assert command in output
    assert "resets after restart" in output and "not all historical" in output


@pytest.mark.parametrize("user_text", ["/unknown", "/help please", "Please /exit later", "/HELP", "Budget reduction"])
def test_only_exact_supported_commands_are_intercepted(user_text, monkeypatch, offline):
    controller = fake_controller()
    inputs(monkeypatch, ["2", "1", user_text, "/exit"])
    TerminalView(controller, offline.console).run()
    controller.send_message.assert_called_once_with("conv_a", user_text)


def test_selection_reprompts_for_unknown_id_and_supports_cancellation(monkeypatch, offline):
    controller = fake_controller()
    inputs(monkeypatch, ["2", "conv_missing", "0", "6"])
    TerminalView(controller, offline.console).run()
    controller.get_conversation.assert_not_called()
    controller.send_message.assert_not_called()
    assert "Select a listed conversation" in offline.output.getvalue()


def test_empty_list_and_blank_default_title(monkeypatch, offline):
    controller = fake_controller()
    controller.list_conversations.return_value = []
    inputs(monkeypatch, ["2", "1", "", "/back", "6"])
    TerminalView(controller, offline.console).run()
    controller.create_conversation.assert_called_once_with(None)
    assert "No conversations found" in offline.output.getvalue()


def test_menu_rename_archive_and_cancelled_delete(monkeypatch, offline):
    controller = fake_controller()
    inputs(monkeypatch, ["3", "1", "New title", "4", "conv_a", "5", "1", "6"])
    confirm = Mock(return_value=False)
    monkeypatch.setattr(Confirm, "ask", confirm)
    TerminalView(controller, offline.console).run()
    controller.rename_conversation.assert_called_once_with("conv_a", "New title")
    controller.archive_conversation.assert_called_once_with("conv_a")
    controller.delete_conversation.assert_not_called()
    assert confirm.call_args.kwargs["default"] is False
    assert "Deletion cancelled" in offline.output.getvalue()


def test_terminal_confirmed_delete_targets_only_selected_conversation(monkeypatch, offline):
    controller = fake_controller()
    controller.list_conversations.return_value.append({
        "id": "conv_b", "title": "Other", "updated_at": "2026-10-25T00:00:00+00:00", "status": "archived",
    })
    inputs(monkeypatch, ["5", "2", "6"])
    confirm = Mock(return_value=True)
    monkeypatch.setattr(Confirm, "ask", confirm)
    TerminalView(controller, offline.console).run()
    assert "conv_b" in confirm.call_args.args[0].plain
    controller.delete_conversation.assert_called_once_with("conv_b", confirmed=True)
    assert "Technical traces retained" in offline.output.getvalue()


def test_deleted_selection_returns_to_menu_without_sending(monkeypatch, offline):
    controller = fake_controller()
    controller.get_conversation.return_value = None
    inputs(monkeypatch, ["2", "1", "6"])
    TerminalView(controller, offline.console).run()
    controller.send_message.assert_not_called()
    assert "Conversation not found" in offline.output.getvalue()


def test_recoverable_send_error_does_not_exit_or_automatically_retry(monkeypatch, offline):
    controller = fake_controller()
    controller.send_message.side_effect = [ControllerError("The AI request failed. Check LLM_MODEL.", "llm"),
                                          OrchestratorResult("conv_a", "Recovered.", "completed", persisted=True)]
    inputs(monkeypatch, ["2", "1", "First message", "Retry explicitly", "/exit"])
    TerminalView(controller, offline.console).run()
    assert controller.send_message.call_count == 2
    assert "LLM_MODEL" in offline.output.getvalue() and "Recovered." in offline.output.getvalue()


def test_persistence_failure_and_partial_output_are_not_presented_as_success(monkeypatch, offline):
    controller = fake_controller()
    controller.send_message.return_value = OrchestratorResult(
        "conv_a", status="error", persisted=False,
        issues=["Memory persistence failed.", "api_key=sk-abcdefghijklmnop"],
        partial_results={"BudgetAgent": "UNVALIDATED_INTERNAL_OUTPUT"},
    )
    inputs(monkeypatch, ["2", "1", "Plan.", "/exit"])
    TerminalView(controller, offline.console).run()
    output = offline.output.getvalue()
    assert "Saving was incomplete" in output and "No validated final response" in output
    assert "Memory persistence failed" in output and "Partial agent work" in output
    assert "UNVALIDATED_INTERNAL_OUTPUT" not in output and "sk-abcdefghijklmnop" not in output


def test_rich_markup_is_literal_in_conversation_content(monkeypatch, offline):
    controller = fake_controller()
    controller.get_conversation.return_value["title"] = "[bold]Event[/bold]"
    controller.get_history.return_value = [{"role": "user", "content": "[link=https://example.com]Message[/link]"}]
    inputs(monkeypatch, ["2", "1", "/exit"])
    TerminalView(controller, offline.console).run()
    output = offline.output.getvalue()
    assert "[bold]Event[/bold]" in output and "[link=https://example.com]Message[/link]" in output


def test_usage_display_marks_missing_cost_and_partial_tokens(monkeypatch, offline):
    llm.USAGE_JOURNAL.extend([
        {"agent": "BudgetAgent", "conversation_id": "conv_a", "request_id": "r1", "prompt_tokens": 10,
         "completion_tokens": 5, "total_tokens": 15, "cost": None},
        {"agent": "BudgetAgent", "conversation_id": "conv_a", "request_id": "r1", "prompt_tokens": None,
         "completion_tokens": None, "total_tokens": None, "cost": None},
    ])
    controller = fake_controller()
    inputs(monkeypatch, ["2", "1", "/usage", "/exit"])
    TerminalView(controller, offline.console).run()
    output = offline.output.getvalue()
    assert "BudgetAgent" in output and "10 (partial)" in output and "unknown" in output
    assert "$0.000000" not in output
    controller.send_message.assert_not_called()


@pytest.mark.parametrize("exception", [KeyboardInterrupt(), EOFError()])
def test_keyboard_interrupt_and_eof_exit_gracefully(exception, monkeypatch, offline):
    inputs(monkeypatch, [exception])
    TerminalView(fake_controller(), offline.console).run()
    assert offline.output.getvalue().count("Goodbye.") == 1


def test_interrupted_message_exits_without_retry(monkeypatch, offline):
    controller = fake_controller()
    controller.send_message.side_effect = KeyboardInterrupt()
    inputs(monkeypatch, ["2", "1", "Plan."])
    TerminalView(controller, offline.console).run()
    controller.send_message.assert_called_once()
    assert "Interrupted" in offline.output.getvalue()


def test_importing_main_does_not_construct_or_start_application(monkeypatch, offline):
    import services
    import view
    constructor = Mock(side_effect=AssertionError("Import must be lazy."))
    terminal = Mock(side_effect=AssertionError("Import must not start the terminal."))
    with monkeypatch.context() as patch:
        patch.setattr(services, "EventOrchestrator", constructor)
        patch.setattr(view, "TerminalView", terminal)
        reload(application)
        constructor.assert_not_called()
        terminal.assert_not_called()
        assert not offline.directory.exists()
    reload(application)


def test_real_main_menu_input_and_exit_without_any_llm_configuration(monkeypatch, offline):
    monkeypatch.setattr("sys.stdin", StringIO("invalid\n6\n"))
    assert application.main() == 0
    assert "EVENT PLANNER AGENT" in offline.output.getvalue()
    assert "Goodbye." in offline.output.getvalue()
    assert trace._listeners == []
    offline.chat.assert_not_called()
    llm.get_client.assert_not_called()
    llm.load_dotenv.assert_not_called()


def test_real_rich_prompts_cover_management_and_delete_confirmation(monkeypatch, offline):
    # Exercise Rich's actual prompt/confirmation parsing without any paid message.
    monkeypatch.setattr("sys.stdin", StringIO(
        "1\nBirthday\n/back\n3\n1\nUpdated title\n4\n1\n5\n1\nn\n5\n1\ny\n6\n"
    ))
    assert application.main() == 0
    output = offline.output.getvalue()
    assert "Conversation renamed" in output and "Conversation archived" in output
    assert "Deletion cancelled" in output and "Conversation deleted" in output
    assert EventController().list_conversations() == []
    offline.chat.assert_not_called()


def test_main_unregisters_listener_even_when_terminal_is_interrupted(monkeypatch, offline):
    terminal = Mock()
    terminal.run.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(application, "TerminalView", Mock(return_value=terminal))
    assert application.main() == 0
    assert trace._listeners == []
    assert "Application stopped" in offline.output.getvalue()


def test_main_uses_existing_trace_renderer_and_cleans_up_listener(monkeypatch, offline):
    observed = []
    trace.define_listener(observed.append)
    terminals = []

    def make_terminal(controller, console):
        assert console is trace.console
        view = Mock()

        def run():
            assert len(trace._listeners) == 2
            trace.info("Orchestrator", "One technical event.", "conv_trace")

        view.run.side_effect = run
        terminals.append(view)
        return view

    monkeypatch.setattr(application, "TerminalView", make_terminal)
    assert application.main() == application.main() == 0
    assert len(trace._listeners) == 1
    assert len(observed) == 2
    dao = EventOrchestrator().trace_dao
    assert len(dao.get_traces("conv_trace")) == 2
    assert offline.output.getvalue().count("One technical event.") == 2
    offline.chat.assert_not_called()


def test_main_can_run_when_trace_file_is_invalid_without_overwriting_it(monkeypatch, offline):
    offline.directory.mkdir()
    path = offline.directory / "logs.json"
    path.write_text("invalid", encoding="utf-8")
    inputs(monkeypatch, ["6"])
    assert application.main() == 0
    assert path.read_text(encoding="utf-8") == "invalid"
    assert "Technical trace persistence is unavailable" in offline.output.getvalue()
    assert "Goodbye." in offline.output.getvalue()
    assert trace._listeners == []


def test_bad_conversation_json_is_reported_and_preserved_in_terminal(monkeypatch, offline):
    offline.directory.mkdir()
    path = offline.directory / "conv.json"
    path.write_text("invalid", encoding="utf-8")
    inputs(monkeypatch, ["2", "6"])
    assert application.main() == 0
    assert path.read_text(encoding="utf-8") == "invalid"
    assert "Conversation storage could not be read or saved" in offline.output.getvalue()
    offline.chat.assert_not_called()


def test_full_terminal_session_persists_clarification_and_continuation_once(monkeypatch, offline):
    offline.chat.side_effect = [*finish("Quelle date ?", "needs_input", ["Date souhaitée ?"]),
                               *finish("Proposition mise à jour.")]
    inputs(monkeypatch, ["1", "Birthday", "Organise un anniversaire.", "Le 30 novembre 2026.", "/history", "/exit"])
    assert application.main() == 0
    controller = EventController()
    conversations = controller.list_conversations()
    assert len(conversations) == 1
    identifier = conversations[0]["id"]
    assert [item["content"] for item in controller.get_history(identifier)] == [
        "Organise un anniversaire.", "Quelle date ?", "Le 30 novembre 2026.", "Proposition mise à jour.",
    ]
    assert {entry.kwargs["conversation_id"] for entry in offline.chat.call_args_list} == {identifier}
    assert controller.orchestrator.trace_dao.get_traces(identifier)


@pytest.mark.parametrize("failure", ["Missing LLM_API_KEY configuration.", "LLM model not found (404).",
                                      "Unable to connect.", "Rate limit reached (429)."])
def test_real_orchestrator_api_failures_remain_recoverable_in_terminal(failure, monkeypatch, offline):
    offline.chat.side_effect = [llm.LLMError(failure), *finish("Recovered.")]
    inputs(monkeypatch, ["1", "Event", "First attempt.", "Try again.", "/exit"])
    assert application.main() == 0
    output = offline.output.getvalue()
    assert "Supervisor execution failed" in output
    assert "LLM_API_KEY" in output and "provider limits" in output
    assert "Recovered." in output
    assert offline.chat.call_count == 3


def test_gembloux_questions_and_budget_change_reach_terminal_after_protocol_repair(monkeypatch, offline):
    first_context = {"requirements": {"attendance": 100, "location": "Gembloux"},
                     "budget": {"total_budget": 10000, "currency": "EUR"}}
    updated_context = first_context | {"budget": {"total_budget": 100000, "currency": "EUR"}}
    offline.chat.side_effect = [
        message(calls=[tool("update_planning_context", {"context": first_context})]),
        finish("Quel type d'événement et quelle date ?", "needs_input", ["Type et date ?"])[0],
        message("Parfait, j'attends vos réponses !"),
        message("Je vais mettre à jour le budget et lancer les spécialistes."),
        message(calls=[tool("update_planning_context", {"context": updated_context})]),
        finish("Le budget est de 100000 EUR. Précisez le type et la date.", "needs_input", ["Type et date ?"])[0],
        message("Merci pour vos précisions."),
    ]
    inputs(monkeypatch, ["1", "Gembloux", "100 personnes, budget 10000 EUR.", "Fais avec 100000.", "/exit"])
    assert application.main() == 0
    output = offline.output.getvalue()
    assert output.count("Status: needs_input") == 2
    assert "Information needed" in output and "Le budget est de 100000 EUR" in output
    assert "Budget cannot be validated" not in output
    assert "No validated final response" not in output
    controller = EventController()
    identifier = controller.list_conversations()[0]["id"]
    assert len(controller.get_history(identifier)) == 4
    assert controller.orchestrator.get_planning_context(identifier)["budget"]["total_budget"] == 100000
