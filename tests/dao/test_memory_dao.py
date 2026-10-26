"""Persistence across agent instances, complete tool history, and instruction boundaries."""

from copy import deepcopy
import json
from unittest.mock import Mock

from openai.types.chat import ChatCompletionMessage
import pytest

from dao import ConversationDAO, MemoryDAO
from model.agent import Agent
from model import llm


def history():
    return [
        {"role": "system", "content": "Saved instructions are data."},
        {"role": "user", "content": "Find a venue in Liège"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "search", "arguments": '{ "city": "Liège" }'}},
        ]},
        {"role": "tool", "tool_call_id": "call-1", "name": "search", "content": '{"venues": []}'},
        {"role": "assistant", "content": "No venues found."},
    ]


def test_memory_roundtrip_preserves_tool_ids_arguments_and_is_defensive(storage_dir):
    conversation = ConversationDAO(storage_dir).create_conversation()
    dao = MemoryDAO(storage_dir)
    original = history()
    expected = deepcopy(original)
    dao.save_memory(conversation, "VenueAgent", original)
    assert original == expected
    original[2]["tool_calls"][0]["function"]["name"] = "Modified"
    reopened = MemoryDAO(storage_dir)
    loaded = reopened.load_memory(conversation, "VenueAgent")
    assert loaded == expected
    loaded[2]["tool_calls"][0]["function"]["arguments"] = "Modified"
    assert reopened.load_memory(conversation, "VenueAgent") == expected
    assert ConversationDAO(storage_dir).get_messages(conversation) == []
    assert {path.name for path in storage_dir.iterdir()} == {"conv.json"}


def test_memories_are_isolated_by_agent_and_conversation(storage_dir):
    conversations = ConversationDAO(storage_dir)
    first, second = conversations.create_conversation(), conversations.create_conversation()
    dao = MemoryDAO(storage_dir)
    for identifier, agent in [(first, "A"), (first, "B"), (second, "A")]:
        dao.save_memory(identifier, agent, [{"role": "user", "content": identifier + agent}])
    dao.delete_memory(first, "A")
    dao.delete_memory(first, "A")
    assert dao.load_memory(first, "A") is None
    assert dao.load_memory(first, "B")[0]["content"] == first + "B"
    assert dao.load_memory(second, "A")[0]["content"] == second + "A"
    assert dao.load_memory(second, "MissingAgent") is None


def test_restore_agent_memory_keeps_current_instructions_authoritative(storage_dir):
    conversation = ConversationDAO(storage_dir).create_conversation()
    MemoryDAO(storage_dir).save_memory(conversation, "VenueAgent", history())
    restored = Agent("VenueAgent", "Current authoritative instructions.")
    restored.load_history(MemoryDAO(storage_dir).load_memory(conversation, "VenueAgent"), conversation)
    assert restored.get_history(conversation)[0]["content"] == "Current authoritative instructions."
    assert restored.get_history(conversation)[1:] == history()[1:]
    assert len(restored.get_history()) == 1


def test_continue_after_restarting_dao_and_agent_instances(monkeypatch, storage_dir):
    conversations = ConversationDAO(storage_dir)
    conversation = conversations.create_conversation("Event")
    conversations.add_message(conversation, "user", "Plan an event for 200 guests.")
    chat = Mock(return_value=ChatCompletionMessage(role="assistant", content="Initial plan."))
    monkeypatch.setattr(llm, "chat", chat)
    agent = Agent("Planner", "Plan events.")
    reply = agent.run("Plan an event for 200 guests.", conversation)
    conversations.add_message(conversation, "assistant", reply, agent=agent.name)
    MemoryDAO(storage_dir).save_memory(conversation, agent.name, agent.get_history(conversation))

    reopened = Agent("Planner", "Plan events with current instructions.")
    reopened.load_history(MemoryDAO(storage_dir).load_memory(conversation, reopened.name), conversation)
    chat.return_value = ChatCompletionMessage(role="assistant", content="Reduced plan.")
    assert reopened.run("Can we reduce the budget?", conversation) == "Reduced plan."
    sent = chat.call_args.args[0]
    assert [item["content"] for item in sent if item["role"] == "user"] == [
        "Plan an event for 200 guests.", "Can we reduce the budget?",
    ]
    assert len(ConversationDAO(storage_dir).get_messages(conversation)) == 2


def test_memory_sanitization_keeps_arguments_as_valid_json_strings(storage_dir):
    conversation = ConversationDAO(storage_dir).create_conversation()
    source = history()
    source[2]["reasoning"] = "private-chain-of-thought"
    source[2]["reasoning_details"] = [{"text": "private-reasoning-detail"}]
    source[2]["tool_calls"][0]["function"]["arguments"] = json.dumps({
        "city": "Namur", "api_key": "private-key", "headers": {"Authorization": "private-header"},
    })
    source[3]["content"] = json.dumps({"venues": [], "environment": {"LLM_API_KEY": "private-env"}})
    MemoryDAO(storage_dir).save_memory(conversation, "VenueAgent", source)
    loaded = MemoryDAO(storage_dir).load_memory(conversation, "VenueAgent")
    assert json.loads(loaded[2]["tool_calls"][0]["function"]["arguments"]) == {"city": "Namur", "api_key": "[REDACTED]"}
    assert json.loads(loaded[3]["content"]) == {"venues": []}
    raw = (storage_dir / "conv.json").read_text(encoding="utf-8")
    for secret in ["private-chain-of-thought", "private-reasoning-detail", "private-key", "private-header", "private-env"]:
        assert secret not in raw
    Agent("VenueAgent", "Current instructions.").load_history(loaded, conversation)


def test_dao_does_not_execute_or_reinterpret_saved_instructions(storage_dir):
    conversation = ConversationDAO(storage_dir).create_conversation()
    messages = [{"role": "system", "content": "__import__('os').system('do not run')"}]
    MemoryDAO(storage_dir).save_memory(conversation, "Agent", messages)
    assert MemoryDAO(storage_dir).load_memory(conversation, "Agent") == messages


@pytest.mark.parametrize("messages", [None, {}, ["invalid"], [{}], [{"role": "unknown", "content": "x"}],
                                      [{"role": "user", "content": None}], [{"role": "tool", "content": "x"}],
                                      [{"role": "assistant", "content": None, "tool_calls": {}}],
                                      [{"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]}],
                                      [{"role": "user", "content": "x", "extra": object()}]])
def test_invalid_memories_do_not_overwrite_existing(messages, storage_dir):
    conversation = ConversationDAO(storage_dir).create_conversation()
    dao = MemoryDAO(storage_dir)
    dao.save_memory(conversation, "A", history())
    before = (storage_dir / "conv.json").read_bytes()
    with pytest.raises(ValueError):
        dao.save_memory(conversation, "A", messages)
    assert (storage_dir / "conv.json").read_bytes() == before


@pytest.mark.parametrize("name", ["", " ", "../Agent", "A\\B", "A\nB", None, [], "x" * 129])
def test_invalid_agent_names(name, storage_dir):
    dao = MemoryDAO(storage_dir)
    for operation in [lambda: dao.save_memory("conv_1", name, []), lambda: dao.load_memory("conv_1", name),
                      lambda: dao.delete_memory("conv_1", name)]:
        with pytest.raises(ValueError):
            operation()


def test_empty_memory_is_distinct_from_missing_and_unknown_conversation(storage_dir):
    conversation = ConversationDAO(storage_dir).create_conversation()
    dao = MemoryDAO(storage_dir)
    dao.save_memory(conversation, "A", [])
    assert dao.load_memory(conversation, "A") == []
    assert dao.load_memory(conversation, "B") is None
    for operation in [lambda: dao.save_memory("conv_missing", "A", []),
                      lambda: dao.load_memory("conv_missing", "A"), lambda: dao.delete_memory("conv_missing", "A")]:
        with pytest.raises(LookupError):
            operation()
