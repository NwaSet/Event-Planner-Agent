"""Synchronous ReAct agents with authorized tools and conversation-local memory.

History is kept only in RAM. Tools are trusted application callables, not sandboxed
code. Calls on one agent should be serialized; export history through get_history()
rather than modifying the public history list. Tool schemas describe arguments,
while domain-specific validation remains the responsibility of each tool.
"""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import inspect
import json
from uuid import uuid4

from model import llm
from model import trace as tracing


@dataclass(frozen=True)
class _RunContext:
    conversation_id: str | None
    request_id: str
    agents: tuple[Agent, ...]


_context: ContextVar[_RunContext | None] = ContextVar("agent_run_context", default=None)
_EMPTY_RESPONSE = "The model returned an empty response; the request could not be completed."


def _conversation_id(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("Conversation ID must be a non-empty string or None.")


def _assistant_record(message) -> dict:
    """Use the LLM history adapter, rejecting malformed envelopes before storage."""
    try:
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
        if tool_calls is not None and not isinstance(tool_calls, list):
            raise ValueError
        record = llm.assistant_message(message)
        if record["content"] is not None and not isinstance(record["content"], str):
            raise ValueError
        if record.get("refusal") is not None and not isinstance(record["refusal"], str):
            raise ValueError
        identifiers = [call["id"] for call in record.get("tool_calls", [])]
        if len(identifiers) != len(set(identifiers)) or any(not value.strip() for value in identifiers):
            raise ValueError
        return record
    except (llm.LLMError, ValueError, TypeError, AttributeError, KeyError):
        raise ValueError("Invalid assistant message or function tool-call structure.") from None


class Agent:
    """A generic agent running a bounded LLM/tool loop with isolated memory.

    history refers to the most recently run, loaded, or cleared conversation.
    None identifies a separate, reusable default conversation. get_history()
    always selects its explicit argument (None by default), not the last run.
    """

    def __init__(self, name: str, instructions: str, tools: list[tuple] | None = None,
                 max_iterations: int = 6):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Agent name must be a non-empty string.")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("Agent instructions must be a non-empty string.")
        if type(max_iterations) is not int or max_iterations <= 0:
            raise ValueError("Maximum iterations must be a positive integer.")
        if tools is not None and not isinstance(tools, list):
            raise ValueError("Tools must be a list of callable/schema tuples or None.")
        self.name = name
        self.instructions = instructions
        self.max_iterations = max_iterations
        self.functions = {}
        self.schemas = []
        self._parameters = {}
        for definition in tools or []:
            if not isinstance(definition, tuple) or len(definition) != 2:
                raise ValueError("Each tool must be a (callable, schema) tuple.")
            function, schema = definition
            if not callable(function) or inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function):
                raise ValueError("Tools must be synchronous callables.")
            try:
                if not isinstance(schema, dict) or schema.get("type") != "function":
                    raise ValueError
                spec = schema["function"]
                parameters = spec["parameters"]
                if parameters.get("type") != "object" or not isinstance(parameters.get("required", []), list):
                    raise ValueError
                # Validate common function-schema fields through the existing API.
                llm.tool_schema(spec["name"], spec["description"], parameters["properties"],
                                parameters.get("required", []))
                json.dumps(schema, allow_nan=False)
                schema = deepcopy(schema)
            except (llm.LLMError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
                raise ValueError("Invalid function tool schema.") from None
            tool_name = spec["name"]
            if tool_name in self.functions:
                raise ValueError("Duplicate tool names are not allowed.")
            self.functions[tool_name] = function
            self.schemas.append(schema)
            self._parameters[tool_name] = schema["function"]["parameters"]
        self._histories: dict[str | None, list[dict]] = {None: self._initial_history()}
        self.history = self._histories[None]

    def _initial_history(self) -> list[dict]:
        return [{"role": "system", "content": self.instructions}]

    def get_history(self, conversation_id: str | None = None) -> list[dict]:
        """Return an independent copy, including nested tool-call dictionaries."""
        _conversation_id(conversation_id)
        return deepcopy(self._histories.get(conversation_id, self._initial_history()))

    def load_history(self, messages: list[dict], conversation_id: str | None = None) -> None:
        """Restore a complete text/function-tool history without executing tools.

        Replace an optional leading saved system message with this agent's own
        instructions. Reject later system/developer messages and incomplete or
        mismatched tool exchanges. Unknown saved tools are retained as history,
        never registered as executable functions. Validation is atomic.
        """
        _conversation_id(conversation_id)
        if not isinstance(messages, list):
            raise ValueError("History must be a list of message dictionaries.")
        restored = self._initial_history()
        pending = {}
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ValueError("Every history message must be a dictionary.")
            role, content = message.get("role"), message.get("content")
            if pending and role != "tool":
                raise ValueError("Assistant tool calls must be followed by all corresponding tool results.")
            if role == "system" and index == 0:
                if not isinstance(content, str):
                    raise ValueError("Saved system message content must be a string.")
                continue
            if role == "user":
                if not isinstance(content, str):
                    raise ValueError("User message content must be a string.")
                restored.append({"role": "user", "content": content})
            elif role == "assistant":
                record = _assistant_record(message)
                pending = {call["id"]: call for call in record.get("tool_calls", [])}
                restored.append(record)
            elif role == "tool":
                identifier = message.get("tool_call_id")
                if not isinstance(identifier, str) or identifier not in pending or not isinstance(content, str):
                    raise ValueError("Tool results must match a pending call and contain string content.")
                call = pending[identifier]
                if message.get("name", call["function"]["name"]) != call["function"]["name"]:
                    raise ValueError("Tool result name does not match the requested function.")
                restored.append(llm.tool_message(call, content))
                del pending[identifier]
            else:
                raise ValueError("Only a leading system message and user, assistant, or tool messages are supported.")
        if pending:
            raise ValueError("Cannot restore history with unresolved tool calls.")
        self._histories[conversation_id] = restored
        self.history = restored

    def clear_history(self, conversation_id: str | None = None) -> None:
        """Reset only the selected conversation to this agent's system prompt."""
        _conversation_id(conversation_id)
        self.history = self._initial_history()
        self._histories[conversation_id] = self.history

    def run(self, request: str, conversation_id: str | None = None) -> str:
        """Run at most max_iterations LLM calls and retain the full tool exchange.

        Each root run has a unique request ID shared with delegated agents.
        LLM failures raise LLMError; ordinary tool failures become observations.
        All context is restored in finally, including on cancellation.
        """
        if not isinstance(request, str) or not request.strip():
            raise ValueError("Request must be a non-empty string.")
        _conversation_id(conversation_id)
        parent = _context.get()
        if parent is not None and self in parent.agents:
            tracing.info(self.name, "Recursive agent delegation was blocked.", conversation_id)
            raise RuntimeError("Recursive agent delegation is not allowed.")
        context = _RunContext(conversation_id, parent.request_id if parent else uuid4().hex,
                              (*parent.agents, self) if parent else (self,))
        token = _context.set(context)
        try:
            history = self._histories.setdefault(conversation_id, self._initial_history())
            self.history = history
            history.append({"role": "user", "content": request})
            tracing.trace(self.name, "ASK", request, conversation_id)
            for _ in range(self.max_iterations):
                try:
                    message = llm.chat(deepcopy(history), tools=deepcopy(self.schemas) or None,
                                       agent=self.name, conversation_id=conversation_id,
                                       request_id=context.request_id)
                except Exception:
                    tracing.error(self.name, "The LLM call failed; the agent could not complete its request.", conversation_id)
                    raise llm.LLMError("The agent could not complete its request because the LLM call failed.") from None
                try:
                    record = _assistant_record(message)
                except ValueError:
                    tracing.error(self.name, "The LLM returned an invalid assistant message.", conversation_id)
                    raise llm.LLMError("The LLM returned an invalid assistant message.") from None
                calls = record.get("tool_calls", [])
                if not calls:
                    text = record["content"]
                    if not text or not text.strip():
                        text = record.get("refusal") or _EMPTY_RESPONSE
                        if not text.strip():
                            text = _EMPTY_RESPONSE
                        record["content"] = text
                        if text == _EMPTY_RESPONSE:
                            tracing.info(self.name, "The LLM returned no usable response.", conversation_id)
                    history.append(record)
                    tracing.response(self.name, text, conversation_id)
                    return text
                # Never trace intermediate assistant content as private reasoning.
                history.append(record)
                recorded = 0
                try:
                    for call in calls:
                        result = self.execute_tool(call)
                        history.append(llm.tool_message(call, result))
                        recorded += 1
                finally:
                    # Keep history valid even when a tool raises KeyboardInterrupt.
                    # A tool may have caused side effects before interruption.
                    for call in calls[recorded:]:
                        history.append(llm.tool_message(call, {
                            "error": "Execution was interrupted; no tool result was recorded. Side effects may have occurred.",
                        }))
            text = f"The operation could not be completed within the limit of {self.max_iterations} LLM iterations."
            history.append({"role": "assistant", "content": text})
            tracing.info(self.name, text, conversation_id)
            return text
        finally:
            _context.reset(token)

    def _tool_error(self, message: str, conversation_id: str | None) -> str:
        tracing.error(self.name, message, conversation_id)
        return tracing.observation(self.name, {"error": message}, conversation_id).summary

    def execute_tool(self, tool_call) -> str:
        """Execute a registered synchronous callable, returning a safe observation.

        Required schema fields and Python keyword binding are checked first.
        Raw exception text is never returned to the model or written to traces.
        Structured results are sanitized by trace.py before becoming observations.
        Outside run(), traces use conversation_id=None.
        """
        context = _context.get()
        conversation_id = context.conversation_id if context else None
        try:
            # The adapter validates the envelope while retaining malformed JSON
            # strings for parse_arguments() to reject without invoking a tool.
            record = _assistant_record({"role": "assistant", "content": None, "tool_calls": [tool_call]})
            call = record["tool_calls"][0]
        except ValueError:
            return self._tool_error("Invalid function tool-call structure.", conversation_id)
        name = call["function"]["name"]
        if name not in self.functions:
            return self._tool_error("The requested tool is not registered for this agent.", conversation_id)
        try:
            arguments = llm.parse_arguments(call)
        except llm.LLMError:
            return self._tool_error("Tool arguments must be a valid JSON object.", conversation_id)
        parameters = self._parameters[name]
        if any(key not in arguments for key in parameters.get("required", [])):
            return self._tool_error("Required tool arguments are missing.", conversation_id)
        if parameters.get("additionalProperties") is False and any(key not in parameters["properties"] for key in arguments):
            return self._tool_error("Unexpected tool arguments were provided.", conversation_id)
        function = self.functions[name]
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            signature = None  # Some built-in callables do not expose a signature.
        if signature is not None:
            try:
                signature.bind(**arguments)
            except TypeError:
                return self._tool_error("Tool arguments do not match the callable's parameters.", conversation_id)
        tracing.action(self.name, name, arguments, conversation_id)
        try:
            result = function(**arguments)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                return self._tool_error("Asynchronous tool results are not supported.", conversation_id)
            # Validate serializability before emitting a successful observation.
            llm.tool_message(call, result)
        except Exception:
            return self._tool_error("Tool execution or result serialization failed; no usable result is available.", conversation_id)
        return tracing.observation(self.name, result, conversation_id).summary

    def as_tool(self, tool_name: str, description: str) -> tuple:
        """Expose this agent using only a required request string in the schema.

        Conversation and request IDs are inherited internally from the parent.
        Direct calls outside an active run use the default None conversation.
        """
        def delegate(request: str) -> str:
            context = _context.get()
            return self.run(request, conversation_id=context.conversation_id if context else None)

        schema = llm.tool_schema(tool_name, description, {
            "request": {"type": "string", "description": "The complete task to delegate to this agent."},
        }, required=["request"])
        return delegate, schema
