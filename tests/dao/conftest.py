"""All DAO tests use temporary storage; real logs and credentials are inaccessible."""

from unittest.mock import Mock

import pytest

from dao import json_storage
from model import llm, trace


@pytest.fixture(autouse=True)
def isolate_dao_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(json_storage, "_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(trace, "_listeners", [])
    monkeypatch.setattr(trace, "_console_enabled", False)
    monkeypatch.setattr(trace, "_colors_agents", {})
    monkeypatch.setattr(llm, "chat", Mock(side_effect=AssertionError("Real LLM calls are forbidden in DAO tests.")))
    monkeypatch.setattr(llm, "OpenAI", Mock(side_effect=AssertionError("DAO tests must not initialize an API client.")))
    monkeypatch.setattr(llm, "load_dotenv", Mock(side_effect=AssertionError("DAO tests must not read .env.")))


@pytest.fixture
def storage_dir(tmp_path):
    return tmp_path / "logs"
