"""Agent history data inside conv.json; no instructions are executed or trusted.

Agent.load_history remains responsible for restoring authoritative system
instructions and checking complete tool exchanges. Saving a memory never adds
visible chat messages, calls an LLM, or summarizes the stored history.
"""

from pathlib import Path

from dao.conversation_dao import _require, _storage, _touch, _validate_memory
from dao.json_storage import json_copy, sanitize_payload, validate_agent_name, validate_identifier


class MemoryDAO:
    def __init__(self, directory: str | Path | None = None):
        self._storage = _storage(directory)

    def save_memory(self, conversation_id: str, agent_name: str, messages: list[dict]) -> None:
        """Save JSON-compatible text/tool history, sanitizing secrets and internal fields."""
        validate_identifier(conversation_id, "Conversation ID")
        validate_agent_name(agent_name)
        messages = json_copy(messages)
        _validate_memory(messages)
        messages = sanitize_payload(messages)

        def save(data):
            conversation = _require(data, conversation_id)
            conversation["agent_memories"][agent_name] = messages
            _touch(conversation)

        self._storage.update(save)

    def load_memory(self, conversation_id: str, agent_name: str) -> list[dict] | None:
        """Return an independent history or None; an unknown conversation raises LookupError."""
        validate_identifier(conversation_id, "Conversation ID")
        validate_agent_name(agent_name)
        return _require(self._storage.read(), conversation_id)["agent_memories"].get(agent_name)

    def delete_memory(self, conversation_id: str, agent_name: str) -> None:
        """Delete only this memory; an absent agent in a known conversation is a no-op."""
        validate_identifier(conversation_id, "Conversation ID")
        validate_agent_name(agent_name)

        def delete(data):
            conversation = _require(data, conversation_id)
            if agent_name in conversation["agent_memories"]:
                del conversation["agent_memories"][agent_name]
                _touch(conversation)

        self._storage.update(delete)
