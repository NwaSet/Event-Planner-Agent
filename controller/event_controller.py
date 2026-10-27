"""Conversation operations without rendering, agent delegation, or duplicate saves.

The controller uses the orchestrator's DAO instance so both layers share the same
storage. Only the orchestrator saves messages and agent memories during a turn.
Operational exceptions become safe ControllerError messages; structured error or
partial orchestration results pass through unchanged for the UI to display.
"""

from dao.json_storage import StorageError, validate_identifier
from model.llm import LLMError
from services import EventOrchestrator, OrchestratorResult


class ControllerError(Exception):
    """A safe application error with a stable code for terminal or future UI use."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class EventController:
    """Stateless conversation selection; reuse this controller across messages.

    Constructing the default orchestrator does not open a client or call an LLM.
    Archived conversations remain readable and may be continued; sending a
    message does not implicitly change their organizational status.
    """

    def __init__(self, orchestrator: EventOrchestrator | None = None):
        self.orchestrator = orchestrator if orchestrator is not None else EventOrchestrator()
        self.conversation_dao = self.orchestrator.conversation_dao

    @staticmethod
    def _call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except ControllerError:
            raise
        except (StorageError, OSError):
            raise ControllerError(
                "Conversation storage could not be read or saved. Check file access and the JSON files; "
                "review history before retrying a message.", "storage",
            ) from None
        except LLMError:
            raise ControllerError(
                "The AI request failed. Check LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, network access, "
                "and provider limits. See the technical traces for available details.", "llm",
            ) from None
        except LookupError:
            raise ControllerError("Conversation not found. Select an existing conversation.", "not_found") from None
        except (ValueError, TypeError):
            raise ControllerError("Invalid input. Check the conversation ID and provide non-empty text.", "invalid_input") from None
        except RuntimeError:
            raise ControllerError("The operation could not run. Wait for the current request to finish before retrying.", "execution") from None
        except Exception:
            # Raw exception text can include credentials or HTTP request details.
            raise ControllerError("The operation failed unexpectedly. Review history before retrying.", "unexpected") from None

    def _require_conversation(self, conversation_id: str) -> dict:
        self._call(validate_identifier, conversation_id, "Conversation ID")
        conversation = self._call(self.conversation_dao.get_conversation, conversation_id)
        if conversation is None:
            raise ControllerError("Conversation not found. Select an existing conversation.", "not_found")
        return conversation

    def create_conversation(self, title: str | None = None) -> str:
        return self._call(self.conversation_dao.create_conversation, title)

    def list_conversations(self) -> list[dict]:
        """Return DAO metadata in most-recently-updated order."""
        return self._call(self.conversation_dao.list_conversations)

    def get_conversation(self, conversation_id: str) -> dict | None:
        """Return visible conversation data, excluding all internal agent memories."""
        self._call(validate_identifier, conversation_id, "Conversation ID")
        conversation = self._call(self.conversation_dao.get_conversation, conversation_id)
        if conversation is None:
            return None
        return {key: value for key, value in conversation.items() if key != "agent_memories"}

    def get_history(self, conversation_id: str) -> list[dict]:
        """Return only persisted user/assistant messages."""
        return self._call(self.conversation_dao.get_messages, conversation_id)

    def send_message(self, conversation_id: str, message: str) -> OrchestratorResult:
        if not isinstance(message, str) or not message.strip():
            raise ControllerError("Enter a non-empty message.", "invalid_input")
        self._require_conversation(conversation_id)
        result = self._call(self.orchestrator.run, request=message, conversation_id=conversation_id)
        if result.conversation_id != conversation_id:
            raise ControllerError("The response belongs to an unexpected conversation; review stored history.", "integration")
        return result

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        self._call(self.conversation_dao.rename_conversation, conversation_id, title)

    def archive_conversation(self, conversation_id: str) -> None:
        self._call(self.conversation_dao.update_status, conversation_id, "archived")

    def delete_conversation(self, conversation_id: str, *, confirmed: bool = False) -> None:
        """Delete exactly one conversation and its memories after explicit approval.

        The UI must collect confirmation. Technical traces are retained.
        """
        if confirmed is not True:
            raise ControllerError("Explicit confirmation is required to delete a conversation.", "confirmation_required")
        self._call(self.conversation_dao.delete_conversation, conversation_id)

    def get_usage(self, conversation_id: str) -> dict:
        """Return this process's recorded usage, not historical lifetime costs."""
        self._require_conversation(conversation_id)
        return self._call(self.orchestrator.usage_summary, conversation_id)
