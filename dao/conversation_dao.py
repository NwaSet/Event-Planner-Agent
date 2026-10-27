"""Conversation metadata and visible chat messages, stored in conv.json.

Unknown IDs return None from get_conversation(); operations requiring a known
conversation raise LookupError. Only user/assistant messages are visible. Agent
tool exchanges are stored separately by MemoryDAO. No status changes are inferred.
"""

from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from dao.json_storage import (JSONStorage, json_copy, parse_timestamp, sanitize_payload,
                              utc_now, validate_agent_name, validate_identifier)


_STATUSES = {"active", "completed", "archived"}
_VISIBLE_ROLES = {"user", "assistant"}


def _text(value, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")


def _utc(value) -> datetime:
    stamp = parse_timestamp(value)
    # Require a written UTC offset, rather than accepting local-time storage.
    if datetime.fromisoformat(value).utcoffset() != timedelta(0):
        raise ValueError("Stored timestamps must be in UTC.")
    return stamp


def _validate_memory(messages: list[dict]) -> None:
    """Validate data shape only; Agent.load_history owns instruction/sequence policy."""
    if not isinstance(messages, list):
        raise ValueError("Agent memory must be a list of message dictionaries.")
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Agent memory messages must be dictionaries.")
        role = message.get("role")
        if not isinstance(role, str) or role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Unsupported agent memory message role.")
        content = message.get("content")
        if not isinstance(content, str) and not (role == "assistant" and content is None):
            raise ValueError("Memory content must be text, or None for assistant tool calls.")
        if "refusal" in message and message["refusal"] is not None and not isinstance(message["refusal"], str):
            raise ValueError("Assistant refusal must be text or None.")
        if role == "tool":
            _text(message.get("tool_call_id"), "Tool-call ID")
            if "name" in message:
                _text(message["name"], "Tool name")
        if "tool_calls" in message:
            if role != "assistant" or not isinstance(message["tool_calls"], list):
                raise ValueError("Only assistant messages may contain a list of tool calls.")
            for call in message["tool_calls"]:
                if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
                    raise ValueError("Memory tool calls must contain function definitions.")
                _text(call.get("id"), "Tool-call ID")
                _text(call["function"].get("name"), "Tool name")
                if not isinstance(call["function"].get("arguments"), str):
                    raise ValueError("Tool-call arguments must remain JSON strings.")


def _validate_conversations(data: dict) -> None:
    seen_messages = set()
    for identifier, conversation in data["conversations"].items():
        validate_identifier(identifier, "Conversation ID")
        if not isinstance(conversation, dict) or conversation.get("id") != identifier:
            raise ValueError("Conversation ID must match its storage key.")
        _text(conversation.get("title"), "Conversation title")
        if not isinstance(conversation.get("status"), str) or conversation["status"] not in _STATUSES:
            raise ValueError("Invalid stored conversation status.")
        created, updated = _utc(conversation.get("created_at")), _utc(conversation.get("updated_at"))
        if updated < created:
            raise ValueError("Conversation update time cannot precede creation.")
        if not isinstance(conversation.get("messages"), list) or not isinstance(conversation.get("agent_memories"), dict):
            raise ValueError("Conversation messages and agent memories have invalid structures.")
        if "event_state" in conversation and not isinstance(conversation["event_state"], dict):
            raise ValueError("Event state must be an object.")
        for message in conversation["messages"]:
            if not isinstance(message, dict):
                raise ValueError("Visible messages must be dictionaries.")
            validate_identifier(message.get("id"), "Message ID")
            if message["id"] in seen_messages:
                raise ValueError("Message IDs must be unique across conversations.")
            seen_messages.add(message["id"])
            if not isinstance(message.get("role"), str) or message["role"] not in _VISIBLE_ROLES:
                raise ValueError("Visible message role must be user or assistant.")
            _text(message.get("content"), "Message content")
            _utc(message.get("timestamp"))
            if message.get("agent") is not None:
                validate_agent_name(message["agent"])
        for agent, history in conversation["agent_memories"].items():
            validate_agent_name(agent)
            _validate_memory(history)


def _storage(directory) -> JSONStorage:
    return JSONStorage("conv.json", directory, validator=_validate_conversations)


def _require(data: dict, conversation_id: str) -> dict:
    conversation = data["conversations"].get(conversation_id)
    if conversation is None:
        raise LookupError("Conversation does not exist.")
    return conversation


def _touch(conversation: dict) -> str:
    # Keep message order and update times monotonic if the wall clock moves back.
    timestamp = max(parse_timestamp(utc_now()), parse_timestamp(conversation["updated_at"])).isoformat()
    conversation["updated_at"] = timestamp
    return timestamp


class ConversationDAO:
    """Visible conversations. directory is optional trusted application configuration."""

    def __init__(self, directory: str | Path | None = None):
        self._storage = _storage(directory)

    def create_conversation(self, title: str | None = None) -> str:
        if title is None:
            title = "New conversation"
        _text(title, "Conversation title")
        title = sanitize_payload(title)

        def create(data):
            identifier = "conv_" + uuid4().hex
            if identifier in data["conversations"]:
                raise ValueError("Generated conversation ID already exists; no conversation was replaced.")
            timestamp = utc_now()
            data["conversations"][identifier] = {
                "id": identifier, "title": title, "created_at": timestamp, "updated_at": timestamp,
                "status": "active", "messages": [], "agent_memories": {},
            }
            return identifier

        return self._storage.update(create)

    def list_conversations(self) -> list[dict]:
        """Return only sidebar metadata, newest update first."""
        conversations = self._storage.read()["conversations"].values()
        keys = ("id", "title", "created_at", "updated_at", "status")
        return [{key: item[key] for key in keys} for item in sorted(
            conversations, key=lambda item: parse_timestamp(item["updated_at"]), reverse=True,
        )]

    def get_conversation(self, conversation_id: str) -> dict | None:
        validate_identifier(conversation_id, "Conversation ID")
        return self._storage.read()["conversations"].get(conversation_id)

    def save_event_state(self, conversation_id: str, state: dict) -> None:
        """Persist the central event dictionary without changing agent histories."""
        validate_identifier(conversation_id, "Conversation ID")
        if not isinstance(state, dict):
            raise ValueError("Event state must be an object.")
        snapshot = sanitize_payload(json_copy(state))

        def save(data):
            conversation = _require(data, conversation_id)
            conversation["event_state"] = snapshot
            _touch(conversation)

        self._storage.update(save)

    def add_message(self, conversation_id: str, role: str, content: str,
                    agent: str | None = None, *, message_id: str | None = None) -> dict:
        """Append a visible message, optionally using message_id as an idempotency key.

        Reusing an ID with the same role/content/agent returns the original message.
        Reusing it for different content or another conversation raises ValueError.
        Without an explicit ID, identical text is a legitimate new message.
        """
        validate_identifier(conversation_id, "Conversation ID")
        if not isinstance(role, str) or role not in _VISIBLE_ROLES:
            raise ValueError("Visible message role must be user or assistant.")
        _text(content, "Message content")
        if agent is not None:
            validate_agent_name(agent)
        identifier = message_id if message_id is not None else "msg_" + uuid4().hex
        validate_identifier(identifier, "Message ID")
        content = sanitize_payload(content)

        def append(data):
            conversation = _require(data, conversation_id)
            for existing_conversation in data["conversations"].values():
                for existing in existing_conversation["messages"]:
                    if existing["id"] == identifier:
                        if (existing_conversation["id"] == conversation_id and existing["role"] == role
                                and existing["content"] == content and existing.get("agent") == agent):
                            return existing
                        raise ValueError("Message ID is already used for a different message.")
            message = {"id": identifier, "role": role, "content": content, "timestamp": _touch(conversation)}
            if agent is not None:
                message["agent"] = agent
            conversation["messages"].append(message)
            return message

        return self._storage.update(append)

    def get_messages(self, conversation_id: str) -> list[dict]:
        validate_identifier(conversation_id, "Conversation ID")
        messages = _require(self._storage.read(), conversation_id)["messages"]
        return sorted(messages, key=lambda message: parse_timestamp(message["timestamp"]))

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        validate_identifier(conversation_id, "Conversation ID")
        _text(title, "Conversation title")
        title = sanitize_payload(title)

        def rename(data):
            conversation = _require(data, conversation_id)
            conversation["title"] = title
            _touch(conversation)

        self._storage.update(rename)

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete the conversation and its memories; technical traces are separate."""
        validate_identifier(conversation_id, "Conversation ID")

        def delete(data):
            _require(data, conversation_id)
            del data["conversations"][conversation_id]

        self._storage.update(delete)

    def update_status(self, conversation_id: str, status: str) -> None:
        validate_identifier(conversation_id, "Conversation ID")
        if not isinstance(status, str) or status not in _STATUSES:
            raise ValueError("Status must be active, completed, or archived.")

        def update(data):
            conversation = _require(data, conversation_id)
            conversation["status"] = status
            _touch(conversation)

        self._storage.update(update)
