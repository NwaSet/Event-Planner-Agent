"""Offline tests for ReAct execution, tool authorization, memory, and delegation."""

from contextvars import Context
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

from openai.types.chat import ChatCompletionMessage
import pytest

from model import agent as agent_module
from model.agent import Agent
from model import llm
from model import trace as tracing


REAL_CHAT = llm.chat


def tool_call(name="search_venues", arguments='{"city":"Namur"}', identifier="call-1"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def message(text="Ready", calls=None, **extra):
    return ChatCompletionMessage(role="assistant", content=text, tool_calls=calls, **extra)


def schema(name="search_venues", required=None):
    return llm.tool_schema(name, "Search venues", {"city": {"type": "string"}}, required)


@pytest.fixture(autouse=True)
def isolated_agent(monkeypatch):
    chat = Mock(return_value=message())
    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "OpenAI", Mock(side_effect=AssertionError("Network access is forbidden in agent tests.")))
    monkeypatch.setattr(llm, "load_dotenv", Mock(side_effect=AssertionError("Agent tests must not read .env.")))
    monkeypatch.setattr(tracing, "_listeners", [])
    monkeypatch.setattr(tracing, "_console_enabled", False)
    monkeypatch.setattr(tracing, "_colors_agents", {})
    token = agent_module._context.set(None)
    events = []
    tracing.define_listener(events.append)
    yield NS(chat=chat, events=events)
    agent_module._context.reset(token)


def test_initialization_and_independent_tool_schema():
    function = Mock()
    definition = schema()
    agent = Agent("VenueAgent", "Find venues.", [(function, definition)], max_iterations=3)
    assert agent.name == "VenueAgent" and agent.instructions == "Find venues."
    assert agent.max_iterations == 3
    assert agent.functions == {"search_venues": function}
    assert agent.schemas == [definition]
    assert agent.history == [{"role": "system", "content": "Find venues."}]
    definition["function"]["parameters"]["properties"]["city"]["type"] = "integer"
    assert agent.schemas[0]["function"]["parameters"]["properties"]["city"]["type"] == "string"


@pytest.mark.parametrize("kwargs", [
    {"name": ""}, {"name": "   "}, {"name": None}, {"instructions": ""}, {"instructions": None},
    {"max_iterations": 0}, {"max_iterations": -1}, {"max_iterations": True}, {"max_iterations": 2.5},
    {"tools": {}}, {"tools": [(None, {})]}, {"tools": [(lambda: None,)]},
    {"tools": [[lambda: None, schema()]]}, {"tools": [(lambda: None, {})]},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        Agent(**({"name": "Agent", "instructions": "Help."} | kwargs))


def test_duplicate_names_and_invalid_schemas():
    function = Mock()
    with pytest.raises(ValueError, match="Duplicate"):
        Agent("A", "Help.", [(function, schema()), (function, schema())])
    for mutate in [
        lambda item: item.update(type="custom"),
        lambda item: item["function"].update(name="bad name"),
        lambda item: item["function"]["parameters"].update(type="array"),
        lambda item: item["function"]["parameters"].update(required=None),
        lambda item: item["function"]["parameters"].update(required=["missing"]),
        lambda item: item["function"]["parameters"].update(properties={"city": "not a schema"}),
        lambda item: item.update(extra=object()),
    ]:
        definition = schema()
        mutate(definition)
        with pytest.raises(ValueError, match="schema"):
            Agent("A", "Help.", [(function, definition)])


def test_async_tools_rejected():
    async def asynchronous(city):
        return city

    with pytest.raises(ValueError, match="synchronous"):
        Agent("A", "Help.", [(asynchronous, schema())])


def test_simple_final_response_no_extra_calls(isolated_agent):
    agent = Agent("Agent", "Help users.")
    isolated_agent.chat.return_value = message("Bonjour à tous !")
    assert agent.run("Planifie une fête", "c1") == "Bonjour à tous !"
    isolated_agent.chat.assert_called_once()
    kwargs = isolated_agent.chat.call_args.kwargs
    assert kwargs["tools"] is None and kwargs["agent"] == "Agent" and kwargs["conversation_id"] == "c1"
    assert kwargs["request_id"]
    assert isolated_agent.chat.call_args.args[0] == [
        {"role": "system", "content": "Help users."}, {"role": "user", "content": "Planifie une fête"},
    ]
    assert [event.action for event in isolated_agent.events] == ["ASK", "RESPONSE"]
    assert all(event.conversation_id == "c1" for event in isolated_agent.events)
    assert agent.history[-1] == {"role": "assistant", "content": "Bonjour à tous !"}


@pytest.mark.parametrize("user_request", ["", "  ", None, 1, []])
def test_invalid_request_does_not_change_memory(user_request, isolated_agent):
    agent = Agent("A", "Help.")
    with pytest.raises(ValueError):
        agent.run(user_request)
    assert agent.history == [{"role": "system", "content": "Help."}]
    isolated_agent.chat.assert_not_called()


@pytest.mark.parametrize("conversation", ["", "  ", [], 1])
def test_invalid_conversation_ids(conversation):
    agent = Agent("A", "Help.")
    for operation in [lambda: agent.run("Hello", conversation), lambda: agent.get_history(conversation),
                      lambda: agent.clear_history(conversation), lambda: agent.load_history([], conversation)]:
        with pytest.raises(ValueError):
            operation()


@pytest.mark.parametrize("content", [None, "", "   "])
def test_empty_response_graceful(content, isolated_agent):
    isolated_agent.chat.return_value = message(content)
    agent = Agent("A", "Help.")
    result = agent.run("Hello")
    assert "could not be completed" in result
    assert agent.history[-1]["content"] == result
    isolated_agent.chat.assert_called_once()
    assert "INFO" in [event.action for event in isolated_agent.events]


def test_refusal_is_preserved(isolated_agent):
    isolated_agent.chat.return_value = message(None, refusal="I cannot perform that action.")
    agent = Agent("A", "Help.")
    assert agent.run("Hello") == "I cannot perform that action."
    assert agent.history[-1]["refusal"] == "I cannot perform that action."


def test_react_loop_single_tool_and_trace_order(isolated_agent):
    search = Mock(return_value={"venues": ["Salle à Liège"]})
    agent = Agent("VenueAgent", "Find venues.", [(search, schema())])
    isolated_agent.chat.side_effect = [message(None, [tool_call()]), message("One venue found.")]
    assert agent.run("Find a venue", "c1") == "One venue found."
    search.assert_called_once_with(city="Namur")
    assert isolated_agent.chat.call_count == 2
    next_history = isolated_agent.chat.call_args_list[1].args[0]
    assert next_history[2]["tool_calls"][0]["id"] == "call-1"
    assert next_history[3] == {"role": "tool", "tool_call_id": "call-1", "name": "search_venues",
                               "content": '{"venues": ["Salle à Liège"]}'}
    assert [event.action for event in isolated_agent.events] == ["ASK", "ACTION", "OBSERV", "RESPONSE"]
    assert all(event.conversation_id == "c1" for event in isolated_agent.events)
    assert len({call.kwargs["request_id"] for call in isolated_agent.chat.call_args_list}) == 1


def test_multiple_tools_per_iteration_preserve_order_and_ids(isolated_agent):
    first, second = Mock(return_value="First"), Mock(return_value=["Second"])
    agent = Agent("A", "Help.", [(first, schema("first")), (second, schema("second"))])
    calls = [tool_call("first", identifier="id-a"), tool_call("second", identifier="id-b")]
    isolated_agent.chat.side_effect = [message(None, calls), message("Done")]
    assert agent.run("Use both") == "Done"
    results = [item for item in agent.history if item["role"] == "tool"]
    assert [item["tool_call_id"] for item in results] == ["id-a", "id-b"]
    assert [item["content"] for item in results] == ["First", '["Second"]']
    assert len(agent.history[2]["tool_calls"]) == 2
    first.assert_called_once()
    second.assert_called_once()


def test_unknown_tool_never_executes_registered_tool(isolated_agent):
    tool = Mock()
    agent = Agent("A", "Help.", [(tool, schema())])
    result = agent.execute_tool(tool_call("__import__"))
    assert "not registered" in json.loads(result)["error"]
    tool.assert_not_called()
    assert [event.action for event in isolated_agent.events] == ["ERROR", "OBSERV"]


@pytest.mark.parametrize("arguments", ["not JSON", "[]", "null", "42", '{"city":NaN}',
                                        "__import__('os').system('anything')"])
def test_malformed_arguments_never_invoke_tool(arguments):
    function = Mock()
    agent = Agent("A", "Help.", [(function, schema())])
    result = agent.execute_tool(tool_call(arguments=arguments))
    assert "valid JSON object" in json.loads(result)["error"]
    function.assert_not_called()


def test_missing_required_parameters_and_signature_mismatch():
    function = Mock()
    agent = Agent("A", "Help.", [(function, schema())])
    assert "missing" in json.loads(agent.execute_tool(tool_call(arguments="{}")))["error"]
    function.assert_not_called()
    executed = []

    def requires_city(city):
        executed.append(city)

    agent = Agent("B", "Help.", [(requires_city, schema(required=[]))])
    for arguments in ["{}", '{"city":"Namur","unexpected":1}']:
        result = agent.execute_tool(tool_call(arguments=arguments))
        assert "parameters" in json.loads(result)["error"]
    assert executed == []


def test_additional_properties_rejected_when_schema_disallows_them():
    definition = schema()
    definition["function"]["parameters"]["additionalProperties"] = False
    function = Mock()
    agent = Agent("A", "Help.", [(function, definition)])
    result = agent.execute_tool(tool_call(arguments='{"city":"Namur","extra":1}'))
    assert "Unexpected" in json.loads(result)["error"]
    function.assert_not_called()


def test_blank_arguments_allowed_for_parameterless_tool():
    function = Mock(return_value="Ready")
    agent = Agent("A", "Help.", [(function, llm.tool_schema("ready", "Get status", {}))])
    assert agent.execute_tool(tool_call("ready", "")) == "Ready"
    function.assert_called_once_with()


@pytest.mark.parametrize("result", [None, 42, True, ["a", "é"], {"city": "Liège"}, "Raw string"])
def test_tool_result_serialization(result):
    function = Mock(return_value=result)
    agent = Agent("A", "Help.", [(function, schema())])
    output = agent.execute_tool(tool_call())
    if isinstance(result, str):
        assert output == result
    else:
        assert json.loads(output) == result


def test_tool_failure_is_safe_observation_and_loop_recovers(isolated_agent):
    function = Mock(side_effect=RuntimeError("unlabeled-private-credential"))
    agent = Agent("A", "Help.", [(function, schema())])
    isolated_agent.chat.side_effect = [message(None, [tool_call()]), message("The venue tool failed.")]
    assert agent.run("Find a venue") == "The venue tool failed."
    observation = next(item for item in agent.history if item["role"] == "tool")
    assert "error" in json.loads(observation["content"])
    assert "unlabeled-private-credential" not in json.dumps(agent.history)
    assert "unlabeled-private-credential" not in json.dumps([event.to_dict() for event in isolated_agent.events])
    assert [event.action for event in isolated_agent.events] == ["ASK", "ACTION", "ERROR", "OBSERV", "RESPONSE"]


def test_nonserializable_tool_result_is_error():
    class PrivateResult:
        def __repr__(self):
            raise AssertionError("An arbitrary representation must not be used.")

    agent = Agent("A", "Help.", [(lambda city: PrivateResult(), schema())])
    assert "serialization failed" in json.loads(agent.execute_tool(tool_call()))["error"]


def test_tool_traces_sanitize_arguments_and_results(isolated_agent):
    function = Mock(return_value={"password": "output-private", "venues": ["Namur"]})
    definition = llm.tool_schema("search", "Search", {"api_key": {"type": "string"}})
    agent = Agent("A", "Help.", [(function, definition)])
    result = agent.execute_tool(tool_call("search", '{"api_key":"input-private"}'))
    function.assert_called_once_with(api_key="input-private")
    assert json.loads(result)["password"] == "[REDACTED]"
    serialized = json.dumps([event.to_dict() for event in isolated_agent.events])
    assert "input-private" not in serialized and "output-private" not in serialized


def test_intermediate_assistant_content_is_not_think_trace(isolated_agent):
    agent = Agent("A", "Help.", [(lambda city: "Ready", schema())])
    isolated_agent.chat.side_effect = [message("Intermediate private reasoning", [tool_call()], reasoning="Hidden reasoning"),
                                       message("Done")]
    agent.run("Hello")
    serialized = json.dumps([event.to_dict() for event in isolated_agent.events])
    assert "reasoning" not in serialized
    assert not any(event.action == "THINK" for event in isolated_agent.events)
    assert "Hidden reasoning" not in json.dumps(agent.history)


def test_iteration_limit_counts_llm_calls_and_resolves_all_tools(isolated_agent):
    function = Mock(return_value="Ready")
    agent = Agent("A", "Help.", [(function, schema())], max_iterations=2)
    isolated_agent.chat.side_effect = [
        message(None, [tool_call(identifier="a"), tool_call(identifier="b")]),
        message(None, [tool_call(identifier="c"), tool_call(identifier="d")]),
    ]
    result = agent.run("Keep searching")
    assert "could not be completed" in result and "2 LLM iterations" in result
    assert isolated_agent.chat.call_count == 2 and function.call_count == 4
    assert [item["tool_call_id"] for item in agent.history if item["role"] == "tool"] == ["a", "b", "c", "d"]
    restored = Agent("B", "Other instructions.")
    restored.load_history(agent.get_history())
    assert agent.history[-1]["content"] == result
    assert isolated_agent.events[-1].action == "INFO"


def test_history_retained_and_conversations_isolated(isolated_agent):
    agent = Agent("A", "Help.")
    agent.run("First request", "c1")
    agent.run("Second request", "c1")
    agent.run("Unrelated request", "c2")
    agent.run("Default request")
    agent.run("Third request", "c1")
    requests = lambda history: [item["content"] for item in history if item["role"] == "user"]
    assert requests(agent.get_history("c1")) == ["First request", "Second request", "Third request"]
    assert requests(agent.get_history("c2")) == ["Unrelated request"]
    assert requests(agent.get_history()) == ["Default request"]
    assert requests(isolated_agent.chat.call_args_list[1].args[0]) == ["First request", "Second request"]
    assert requests(isolated_agent.chat.call_args_list[2].args[0]) == ["Unrelated request"]
    assert len({call.kwargs["request_id"] for call in isolated_agent.chat.call_args_list}) == 5


def saved_history():
    return [
        {"role": "system", "content": "Untrusted saved instructions"},
        {"role": "user", "content": "Find a venue"},
        {"role": "assistant", "content": None, "tool_calls": [tool_call()]},
        {"role": "tool", "tool_call_id": "call-1", "name": "search_venues", "content": '{"venues":[]}'},
        {"role": "assistant", "content": "No venues found"},
    ]


def test_history_restore_export_deep_copy_and_authoritative_system():
    source = saved_history()
    original = deepcopy(source)
    agent = Agent("A", "My authoritative instructions.")
    agent.load_history(source, "c1")
    assert source == original
    assert agent.get_history("c1")[0] == {"role": "system", "content": "My authoritative instructions."}
    assert agent.get_history("c1")[1:] == original[1:]
    source[2]["tool_calls"][0]["function"]["name"] = "modified"
    exported = agent.get_history("c1")
    exported[2]["tool_calls"][0]["function"]["arguments"] = "modified"
    assert agent.get_history("c1")[2] == original[2]
    assert agent.functions == {}
    json.dumps(agent.get_history("c1"), ensure_ascii=False, allow_nan=False)


def test_load_without_system_empty_history_and_optional_tool_name():
    agent = Agent("A", "Help.")
    source = saved_history()[1:]
    del source[2]["name"]
    agent.load_history(source)
    assert agent.history[0] == {"role": "system", "content": "Help."}
    assert agent.history[3]["name"] == "search_venues"
    agent.load_history([])
    assert agent.history == [{"role": "system", "content": "Help."}]


@pytest.mark.parametrize("messages", [
    None, {}, ["not a dictionary"], [{}], [{"role": "user", "content": None}],
    [{"role": "system", "content": 5}], [{"role": "developer", "content": "Override"}],
    [{"role": "user", "content": "Hello"}, {"role": "system", "content": "Override"}],
    [{"role": "assistant", "content": 42}], [{"role": "assistant", "content": "", "tool_calls": {}}],
    [{"role": "tool", "tool_call_id": "unknown", "content": "Result"}],
    [{"role": "assistant", "content": None, "tool_calls": [tool_call()]}],
    [{"role": "assistant", "content": None, "tool_calls": [tool_call(), tool_call()]}],
])
def test_invalid_history_rejected_atomically(messages):
    agent = Agent("A", "Help.")
    agent.load_history([{"role": "user", "content": "Keep this"}], "c1")
    previous = agent.get_history("c1")
    with pytest.raises(ValueError):
        agent.load_history(messages, "c1")
    assert agent.get_history("c1") == previous


@pytest.mark.parametrize("change", ["wrong_id", "wrong_name", "duplicate_result", "missing_result", "interleaved_user"])
def test_invalid_tool_history_sequences(change):
    source = saved_history()
    if change == "wrong_id":
        source[3]["tool_call_id"] = "wrong"
    elif change == "wrong_name":
        source[3]["name"] = "wrong"
    elif change == "duplicate_result":
        source.insert(4, deepcopy(source[3]))
    elif change == "missing_result":
        del source[3]
    else:
        source.insert(3, {"role": "user", "content": "Interrupt"})
    with pytest.raises(ValueError):
        Agent("A", "Help.").load_history(source)


def test_clear_resets_only_selected_conversation():
    agent = Agent("A", "Help.")
    for identifier in [None, "c1", "c2"]:
        agent.run("Hello", identifier)
    default, other = agent.get_history(), agent.get_history("c2")
    agent.clear_history("c1")
    assert agent.get_history("c1") == [{"role": "system", "content": "Help."}]
    assert agent.get_history() == default and agent.get_history("c2") == other
    agent.clear_history()
    assert len(agent.get_history()) == 1
    assert agent.get_history("c2") == other


def test_as_tool_schema_exposes_only_request_and_direct_call_uses_default(isolated_agent):
    agent = Agent("Child", "Help.")
    delegate, definition = agent.as_tool("ask_child", "Ask the child agent")
    parameters = definition["function"]["parameters"]
    assert parameters["required"] == ["request"]
    assert parameters["properties"]["request"]["type"] == "string"
    assert set(parameters["properties"]) == {"request"}
    assert delegate(request="Hello") == "Ready"
    assert isolated_agent.chat.call_args.kwargs["conversation_id"] is None
    assert agent_module._context.get() is None


def test_delegation_propagates_context_memory_traces_and_request_id(isolated_agent):
    leaf = Agent("Leaf", "Find venues.", [(lambda city: {"venues": [city]}, schema())])
    child = Agent("Child", "Ask the leaf.", [leaf.as_tool("ask_leaf", "Ask leaf")])
    parent = Agent("Parent", "Delegate tasks.", [child.as_tool("ask_child", "Ask child")])
    isolated_agent.chat.side_effect = [
        message(None, [tool_call("ask_child", '{"request":"Find a venue"}', "parent-call")]),
        message(None, [tool_call("ask_leaf", '{"request":"Search Namur"}', "child-call")]),
        message(None, [tool_call()]), message("Leaf result"), message("Child result"), message("Parent result"),
    ]
    assert parent.run("Plan an event", "shared-conversation") == "Parent result"
    calls = isolated_agent.chat.call_args_list
    assert [call.kwargs["agent"] for call in calls] == ["Parent", "Child", "Leaf", "Leaf", "Child", "Parent"]
    assert all(call.kwargs["conversation_id"] == "shared-conversation" for call in calls)
    assert len({call.kwargs["request_id"] for call in calls}) == 1
    assert all(event.conversation_id == "shared-conversation" for event in isolated_agent.events)
    for agent in [parent, child, leaf]:
        assert len(agent.get_history("shared-conversation")) > 1
        assert len(agent.get_history()) == 1
    assert parent.get_history("shared-conversation")[3]["content"] == "Child result"
    assert child.get_history("shared-conversation")[3]["content"] == "Leaf result"
    assert agent_module._context.get() is None


def test_delegated_failure_restores_parent_context_and_becomes_tool_error(isolated_agent):
    child = Agent("Child", "Help.")
    parent = Agent("Parent", "Delegate.", [child.as_tool("ask_child", "Ask child")])
    isolated_agent.chat.side_effect = [
        message(None, [tool_call("ask_child", '{"request":"Help"}')]),
        llm.LLMError("private-error-text"), message("Delegation failed."),
    ]
    assert parent.run("Help", "c1") == "Delegation failed."
    assert "error" in json.loads(parent.get_history("c1")[3]["content"])
    assert all(event.conversation_id == "c1" for event in isolated_agent.events)
    assert "private-error-text" not in json.dumps([event.to_dict() for event in isolated_agent.events])
    assert agent_module._context.get() is None
    isolated_agent.chat.side_effect = None
    child.as_tool("ask_child", "Ask child")[0]("New independent request")
    assert isolated_agent.chat.call_args.kwargs["conversation_id"] is None


def test_direct_recursion_blocked(isolated_agent):
    agent = Agent("A", "Help.")

    def loop(city):
        return agent.as_tool("ask_self", "Ask self")[0]("Repeat")

    agent = Agent("A", "Help.", [(loop, schema())])
    isolated_agent.chat.side_effect = [message(None, [tool_call()]), message("Recursion blocked.")]
    assert agent.run("Start", "c1") == "Recursion blocked."
    assert isolated_agent.chat.call_count == 2
    assert any("Recursive" in event.summary for event in isolated_agent.events)
    assert "error" in json.loads(agent.history[3]["content"])


def test_context_is_execution_local_and_restored_after_error(isolated_agent):
    sentinel = agent_module._RunContext("outer", "outer-request", ())
    token = agent_module._context.set(sentinel)
    try:
        assert Context().run(agent_module._context.get) is None
        isolated_agent.chat.side_effect = RuntimeError("secret-do-not-report")
        with pytest.raises(llm.LLMError) as failure:
            Agent("A", "Help.").run("Hello", "inner")
        assert "secret-do-not-report" not in str(failure.value)
        assert agent_module._context.get() is sentinel
    finally:
        agent_module._context.reset(token)


@pytest.mark.parametrize("failure", [llm.LLMError("private-error"), RuntimeError("private-error")])
def test_llm_failures_propagate_without_fabricated_response(failure, isolated_agent):
    isolated_agent.chat.side_effect = failure
    agent = Agent("A", "Help.")
    with pytest.raises(llm.LLMError) as exc:
        agent.run("Hello", "c1")
    assert "private-error" not in str(exc.value)
    assert exc.value.__suppress_context__
    assert [item["role"] for item in agent.get_history("c1")] == ["system", "user"]
    assert [event.action for event in isolated_agent.events] == ["ASK", "ERROR"]
    assert agent_module._context.get() is None


def test_malformed_model_tool_calls_do_not_corrupt_history(isolated_agent):
    isolated_agent.chat.return_value = NS(role="assistant", content=None, tool_calls=[tool_call(), tool_call()])
    agent = Agent("A", "Help.")
    with pytest.raises(llm.LLMError, match="invalid assistant"):
        agent.run("Hello")
    assert len(agent.history) == 2
    assert agent_module._context.get() is None


def test_interrupted_tool_batch_has_results_for_every_call(isolated_agent):
    first, interrupt, skipped = Mock(return_value="Done"), Mock(side_effect=KeyboardInterrupt()), Mock()
    agent = Agent("A", "Help.", [(first, schema("first")), (interrupt, schema("interrupt")), (skipped, schema("skipped"))])
    isolated_agent.chat.return_value = message(None, [
        tool_call("first", identifier="a"), tool_call("interrupt", identifier="b"), tool_call("skipped", identifier="c"),
    ])
    with pytest.raises(KeyboardInterrupt):
        agent.run("Start", "c1")
    assert agent_module._context.get() is None
    results = [item for item in agent.get_history("c1") if item["role"] == "tool"]
    assert [item["tool_call_id"] for item in results] == ["a", "b", "c"]
    assert results[0]["content"] == "Done"
    assert all("interrupted" in json.loads(item["content"])["error"] for item in results[1:])
    skipped.assert_not_called()
    Agent("Restored", "Help.").load_history(agent.get_history("c1"))


def test_real_llm_accounting_integration_without_network(monkeypatch, isolated_agent):
    responses = [message(None, [tool_call()]), message("Done")]
    create = Mock(side_effect=[NS(id=f"gen-{index}", model="mock-model", choices=[NS(message=item)],
                                 usage=NS(prompt_tokens=10, completion_tokens=2, total_tokens=12, cost=0.01))
                               for index, item in enumerate(responses)])
    monkeypatch.setattr(llm, "chat", REAL_CHAT)
    monkeypatch.setattr(llm, "_client", NS(api_key="fake-key", chat=NS(completions=NS(create=create))))
    monkeypatch.setattr(llm, "_model", "mock-model")
    monkeypatch.setattr(llm, "USAGE_JOURNAL", [])
    agent = Agent("A", "Help.", [(lambda city: "Found", schema())])
    assert agent.run("Find a venue", "c1") == "Done"
    assert len(llm.USAGE_JOURNAL) == 2
    assert len({entry["request_id"] for entry in llm.USAGE_JOURNAL}) == 1
    assert llm.usage_summary("c1")["total"]["total_tokens"] == 24
    actions = [event.action for event in isolated_agent.events]
    assert actions.count("LLM") == actions.count("TOKEN") == actions.count("COST") == 2


def test_indirect_delegation_cycle_is_blocked(isolated_agent):
    def return_to_parent(city):
        return parent.as_tool("ask_parent", "Ask parent")[0]("Repeat")

    child = Agent("Child", "Help.", [(return_to_parent, schema())])
    parent = Agent("Parent", "Delegate.", [child.as_tool("ask_child", "Ask child")])
    isolated_agent.chat.side_effect = [
        message(None, [tool_call("ask_child", '{"request":"Help"}')]),
        message(None, [tool_call()]), message("Cycle blocked."), message("Parent finished."),
    ]
    assert parent.run("Start", "c1") == "Parent finished."
    assert isolated_agent.chat.call_count == 4
    assert "error" in json.loads(child.get_history("c1")[3]["content"])
    assert any("Recursive" in event.summary for event in isolated_agent.events)
    assert agent_module._context.get() is None
