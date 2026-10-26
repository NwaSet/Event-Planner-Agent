"""Atomic UTF-8 JSON documents with shared, per-path process-local locks.

Only conv.json and logs.json are supported. directory is trusted application
configuration (including temporary test directories), never an LLM argument.
Missing files are initialized on the first read or update. Existing empty,
malformed, or incompatible files are rejected, never silently reset.

Locks cover the entire read/modify/write operation across DAO instances in ONE
Python process. Independent processes must not write these files concurrently.
Whole documents are rewritten; this is storage for a small local application.
"""

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from threading import Lock, RLock
from typing import TypeVar

from model.trace import TraceEvent


_T = TypeVar("_T")
_LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"
_COLLECTIONS = {"conv.json": ("conversations", dict), "logs.json": ("events", list)}
_LOCKS: dict[str, RLock] = {}
_LOCKS_GUARD = Lock()
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class StorageError(Exception):
    """A safe, local persistence error without raw document contents."""


class StorageFormatError(StorageError):
    """An existing document is empty, malformed, or has an unsupported schema."""


class StorageIOError(StorageError):
    """A storage file could not be read or atomically replaced."""


def _json_values(value) -> None:
    """Require native JSON types so keys and numeric values cannot silently change."""
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_values(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_values(item)
        return
    raise ValueError("Storage values must use native JSON types, string keys, and finite numbers.")


def json_copy(value):
    """Validate and copy caller-owned JSON data without normalizing its contents."""
    try:
        _json_values(value)
        return deepcopy(value)
    except (RecursionError, TypeError):
        raise ValueError("Storage values must be finite, acyclic JSON data.") from None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object keys are not supported.")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite JSON constants are not supported.")


def validate_identifier(value: str, label: str = "Identifier") -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must contain 1 to 128 letters, digits, underscores, or hyphens.")


def validate_agent_name(value: str) -> None:
    if (not isinstance(value, str) or not value.strip() or len(value) > 128
            or value in {".", ".."} or any(ord(char) < 32 or char in "/\\" for char in value)):
        raise ValueError("Agent name must be non-empty, at most 128 characters, and contain no path separators or control characters.")
    if sanitize_payload(value) != value:
        raise ValueError("Agent name must not contain credential patterns.")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Timestamp must be a valid timezone-aware ISO 8601 datetime.") from None


_OMIT_FIELDS = {
    "authorization", "proxyauthorization", "headers", "requestheaders", "responseheaders",
    "env", "environ", "environment", "environmentvariables", "rawenvironment",
    "reasoning", "reasoningdetails", "chainofthought", "privatechainofthought", "internalreasoning",
}
_SECRET_PARTS = ("apikey", "password", "passwd", "secret", "accesstoken", "refreshtoken",
                 "idtoken", "authtoken", "credential", "cookie")
_ENV_TEXT = re.compile(
    r"\b(?P<key>(?:LLM|OPENAI)_[A-Z0-9_]+)\s*=\s*"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)", re.IGNORECASE,
)


def sanitize_payload(value):
    """Copy JSON payloads, dropping internal reasoning, headers, and environment maps.

    Credential fields are redacted. JSON strings (notably function arguments and
    tool results) are decoded for sanitization and remain strings; unchanged JSON
    retains its original formatting. Free-text protection is best effort, using
    trace.py's public serializer. Callers must never submit unmarked secrets or
    private reasoning as ordinary message text. No environment values are read.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _OMIT_FIELDS:
                continue
            if normalized == "token" or any(part in normalized for part in _SECRET_PARTS):
                result[key] = "[REDACTED]"
            else:
                result[key] = sanitize_payload(item)
        return result
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value, object_pairs_hook=_object, parse_constant=_constant)
                _json_values(decoded)
            except (ValueError, RecursionError):
                pass
            else:
                cleaned = sanitize_payload(decoded)
                return value if cleaned == decoded else json.dumps(cleaned, ensure_ascii=False, allow_nan=False)
        text = _ENV_TEXT.sub(lambda match: match["key"] + "=[REDACTED]", value)
        return TraceEvent("", None, "Persistence", "INFO", text, {}).to_dict()["summary"]
    return value


class JSONStorage:
    """Read, replace, and atomically update one fixed application document.

    An update callback edits a fresh document in place and may return a result.
    Callback/validation failures leave the file unchanged. Callbacks must not
    recursively update storage files. Reads and results are independent copies.
    """

    def __init__(self, filename: str, directory: str | Path | None = None,
                 *, validator: Callable[[dict], None] | None = None):
        if not isinstance(filename, str) or filename not in _COLLECTIONS:
            raise ValueError("Storage filename must be conv.json or logs.json.")
        self.path = (Path(directory) if directory is not None else _LOGS_DIR).resolve() / filename
        self._validator = validator
        key = os.path.normcase(str(self.path))
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, RLock())

    def _validate(self, data: dict) -> None:
        collection, kind = _COLLECTIONS[self.path.name]
        if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
                or not isinstance(data.get(collection), kind)):
            raise ValueError("Unsupported storage document structure or version.")
        if self._validator is not None:
            self._validator(data)

    def _read(self) -> tuple[dict, bool]:
        if self.path.is_symlink():
            raise StorageIOError("Storage files must not be symbolic links.")
        try:
            with self.path.open("r", encoding="utf-8") as source:
                data = json.load(source, object_pairs_hook=_object, parse_constant=_constant)
            _json_values(data)
            self._validate(data)
            return data, True
        except FileNotFoundError:
            collection, kind = _COLLECTIONS[self.path.name]
            return {"version": 1, collection: kind()}, False
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise StorageFormatError(f"{self.path.name} is empty, malformed, or incompatible; existing data was not changed.") from None
        except OSError:
            raise StorageIOError(f"Unable to read {self.path.name}.") from None

    def _write(self, data: dict) -> None:
        data = json_copy(data)
        self._validate(data)
        try:
            payload = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("Storage data cannot be serialized as UTF-8 JSON.") from None
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{self.path.name}.", suffix=".tmp",
                                             dir=self.path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise StorageIOError(f"Unable to atomically write {self.path.name}; the replacement did not complete.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # A denied cleanup may leave an unreferenced temporary file.

    def read(self) -> dict:
        """Read a validated document, creating the empty structure only if missing."""
        with self._lock:
            data, exists = self._read()
            if not exists:
                self._write(data)
            return deepcopy(data)

    def write(self, data: dict) -> None:
        """Explicitly replace a whole valid document; use update() for mutations."""
        candidate = json_copy(data)
        with self._lock:
            self._read()  # Refuse to replace malformed existing files.
            self._write(candidate)

    def update(self, mutate: Callable[[dict], _T]) -> _T:
        """Serialize a complete read/modify/write transaction within this process."""
        if not callable(mutate):
            raise TypeError("Update callback must be callable.")
        with self._lock:
            data, exists = self._read()
            previous = deepcopy(data)
            result = mutate(data)
            result = deepcopy(result)
            if not exists or data != previous:
                self._write(data)
            return result
