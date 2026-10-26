"""Visible chat CRUD, idempotency, sidebar metadata, and concurrent updates."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from dao import ConversationDAO, MemoryDAO, TraceDAO
from dao import conversation_dao as module
from dao.json_storage import StorageFormatError
from model.trace import TraceEvent


def test_create_unique_conversations_with_utc_timestamps_and_default_title(storage_dir):
    dao = ConversationDAO(storage_dir)
    first, second = dao.create_conversation("Fête à Liège"), dao.create_conversation()
    assert first != second
    conversation = dao.get_conversation(first)
    assert conversation["title"] == "Fête à Liège"
    assert conversation["status"] == "active"
    assert conversation["messages"] == [] and conversation["agent_memories"] == {}
    assert conversation["created_at"] == conversation["updated_at"]
    assert datetime.fromisoformat(conversation["created_at"]).utcoffset() == timedelta(0)
    assert dao.get_conversation(second)["title"] == "New conversation"
    assert list(storage_dir.iterdir()) == [storage_dir / "conv.json"]


def test_list_sidebar_metadata_sorted_by_updates(monkeypatch, storage_dir):
    times = iter(["2026-10-26T09:00:00+00:00", "2026-10-26T09:01:00+00:00", "2026-10-26T09:02:00+00:00"])
    monkeypatch.setattr(module, "utc_now", lambda: next(times))
    dao = ConversationDAO(storage_dir)
    first, second = dao.create_conversation("First"), dao.create_conversation("Second")
    assert [item["id"] for item in dao.list_conversations()] == [second, first]
    dao.add_message(first, "user", "Hello")
    sidebar = dao.list_conversations()
    assert [item["id"] for item in sidebar] == [first, second]
    assert set(sidebar[0]) == {"id", "title", "created_at", "updated_at", "status"}
    sidebar[0]["title"] = "Changed locally"
    assert dao.get_conversation(first)["title"] == "First"


def test_visible_messages_and_restart_instances(storage_dir):
    dao = ConversationDAO(storage_dir)
    identifier = dao.create_conversation("Planning")
    first = dao.add_message(identifier, "user", "Bonjour à Namur")
    second = dao.add_message(identifier, "assistant", "Je peux vous aider.", agent="Orchestrator")
    assert first["id"] != second["id"]
    assert "agent" not in first and second["agent"] == "Orchestrator"
    reopened = ConversationDAO(storage_dir)
    assert reopened.get_messages(identifier) == [first, second]
    assert reopened.get_conversation(identifier)["status"] == "active"
    assert reopened.get_conversation(identifier)["updated_at"] >= second["timestamp"]
    first["content"] = "Changed locally"
    messages = reopened.get_messages(identifier)
    messages[0]["content"] = "Another local change"
    assert reopened.get_messages(identifier)[0]["content"] == "Bonjour à Namur"


def test_get_messages_sorts_by_timestamp_not_file_order(storage_dir):
    dao = ConversationDAO(storage_dir)
    identifier = dao.create_conversation()
    dao.add_message(identifier, "user", "First")
    dao.add_message(identifier, "assistant", "Second")
    dao._storage.update(lambda data: data["conversations"][identifier]["messages"].reverse())
    assert [message["content"] for message in dao.get_messages(identifier)] == ["First", "Second"]


def test_rename_status_and_no_automatic_completion(storage_dir):
    dao = ConversationDAO(storage_dir)
    identifier = dao.create_conversation()
    dao.rename_conversation(identifier, "Concert à Namur")
    assert dao.get_conversation(identifier)["title"] == "Concert à Namur"
    dao.add_message(identifier, "assistant", "Here is one response.")
    assert dao.get_conversation(identifier)["status"] == "active"
    for status in ["completed", "archived", "active"]:
        dao.update_status(identifier, status)
        assert ConversationDAO(storage_dir).get_conversation(identifier)["status"] == status


def test_delete_one_conversation_includes_memory_but_preserves_logs(storage_dir):
    dao, memory, traces = ConversationDAO(storage_dir), MemoryDAO(storage_dir), TraceDAO(storage_dir)
    first, second = dao.create_conversation(), dao.create_conversation()
    memory.save_memory(first, "A", [{"role": "user", "content": "Saved"}])
    traces.save_trace(TraceEvent("2026-10-26T10:00:00+00:00", first, "A", "INFO", "Recorded", {}))
    other = dao.get_conversation(second)
    dao.delete_conversation(first)
    assert dao.get_conversation(first) is None and dao.get_conversation(second) == other
    assert len(traces.get_traces(first)) == 1
    with pytest.raises(LookupError):
        memory.load_memory(first, "A")


def test_message_idempotency_and_conflicting_reuse(storage_dir):
    dao = ConversationDAO(storage_dir)
    identifier = dao.create_conversation()
    first = dao.add_message(identifier, "user", "Hello", message_id="msg_retry")
    before = (storage_dir / "conv.json").read_bytes()
    assert dao.add_message(identifier, "user", "Hello", message_id="msg_retry") == first
    assert (storage_dir / "conv.json").read_bytes() == before
    with pytest.raises(ValueError, match="already used"):
        dao.add_message(identifier, "user", "Different content", message_id="msg_retry")
    other = dao.create_conversation()
    with pytest.raises(ValueError, match="already used"):
        dao.add_message(other, "user", "Hello", message_id="msg_retry")
    dao.add_message(identifier, "user", "Hello")
    assert len(dao.get_messages(identifier)) == 2  # Repeated text is legitimate with a new ID.


def test_concurrent_message_retries_insert_once(storage_dir):
    identifier = ConversationDAO(storage_dir).create_conversation()

    def retry(index):
        return ConversationDAO(storage_dir).add_message(identifier, "user", "Hello", message_id="msg_retry")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(retry, range(12)))
    assert all(result == results[0] for result in results)
    assert len(ConversationDAO(storage_dir).get_messages(identifier)) == 1


def test_concurrent_messages_and_memories_share_one_transaction_lock(storage_dir):
    identifier = ConversationDAO(storage_dir).create_conversation()

    def save(index):
        if index % 2:
            ConversationDAO(storage_dir).add_message(identifier, "user", f"Message {index}")
        else:
            MemoryDAO(storage_dir).save_memory(identifier, f"Agent{index}", [{"role": "user", "content": str(index)}])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(24)))
    conversation = ConversationDAO(storage_dir).get_conversation(identifier)
    assert len(conversation["messages"]) == len(conversation["agent_memories"]) == 12
    assert len({message["id"] for message in conversation["messages"]}) == 12


def test_unknown_conversation_operations(storage_dir):
    dao = ConversationDAO(storage_dir)
    assert dao.get_conversation("conv_unknown") is None
    for operation in [lambda: dao.add_message("conv_unknown", "user", "Hello"),
                      lambda: dao.get_messages("conv_unknown"), lambda: dao.rename_conversation("conv_unknown", "Title"),
                      lambda: dao.delete_conversation("conv_unknown"), lambda: dao.update_status("conv_unknown", "active")]:
        with pytest.raises(LookupError):
            operation()
    assert dao.list_conversations() == []


@pytest.mark.parametrize("identifier", ["", "../conv", "..\\conv", "C:\\secret", None, [], "x" * 129])
def test_invalid_conversation_ids(identifier, storage_dir):
    with pytest.raises(ValueError):
        ConversationDAO(storage_dir).get_conversation(identifier)
    assert not storage_dir.exists()


@pytest.mark.parametrize("kwargs", [{"role": "tool"}, {"role": "system"}, {"role": []},
                                     {"content": ""}, {"content": "  "}, {"content": None},
                                     {"agent": "../Agent"}, {"agent": ""}, {"message_id": "../message"}])
def test_invalid_visible_messages(kwargs, storage_dir):
    dao = ConversationDAO(storage_dir)
    identifier = dao.create_conversation()
    with pytest.raises(ValueError):
        dao.add_message(identifier, **({"role": "user", "content": "Hello"} | kwargs))
    assert dao.get_messages(identifier) == []


def test_invalid_titles_status_and_generated_id_collision(monkeypatch, storage_dir):
    dao = ConversationDAO(storage_dir)
    for title in ["", "  ", 5]:
        with pytest.raises(ValueError):
            dao.create_conversation(title)
    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex="collision"))
    identifier = dao.create_conversation("Original")
    with pytest.raises(ValueError, match="already exists"):
        dao.create_conversation("Replacement")
    assert dao.get_conversation(identifier)["title"] == "Original"
    for status in ["unknown", "", None, []]:
        with pytest.raises(ValueError):
            dao.update_status(identifier, status)


def test_valid_extensions_survive_updates_and_incompatible_schema_is_not_overwritten(storage_dir):
    dao = ConversationDAO(storage_dir)
    identifier = dao.create_conversation()
    dao._storage.update(lambda data: data.update(extension={"keep": True}))
    dao.rename_conversation(identifier, "Changed")
    assert dao._storage.read()["extension"] == {"keep": True}
    path = storage_dir / "conv.json"
    raw = json.dumps({"version": 1, "conversations": {"invalid": {"title": "Incomplete"}}})
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(StorageFormatError):
        dao.create_conversation()
    assert path.read_text(encoding="utf-8") == raw


def test_visible_text_secret_redaction_preserves_normal_user_content(storage_dir):
    dao = ConversationDAO(storage_dir)
    identifier = dao.create_conversation("Birthday in Namur")
    normal = "Please plan a birthday for 100 guests."
    assert dao.add_message(identifier, "user", normal)["content"] == normal
    dao.add_message(identifier, "user", "LLM_API_KEY=private-env-key password='private-password'")
    raw = (storage_dir / "conv.json").read_text(encoding="utf-8")
    assert "private-env-key" not in raw and "private-password" not in raw
