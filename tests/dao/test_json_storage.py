"""Atomic replacement, malformed-data protection, and process-local concurrency."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import pytest

from dao import json_storage as module
from dao.json_storage import JSONStorage, StorageFormatError, StorageIOError


@pytest.mark.parametrize("filename,empty", [("conv.json", {"version": 1, "conversations": {}}),
                                          ("logs.json", {"version": 1, "events": []})])
def test_missing_storage_initialized_on_read(filename, empty, storage_dir):
    storage = JSONStorage(filename, storage_dir)
    assert not storage_dir.exists()
    assert storage.read() == empty
    assert json.loads(storage.path.read_text(encoding="utf-8")) == empty
    assert list(storage_dir.iterdir()) == [storage.path]


def test_utf8_read_write_and_defensive_copies(storage_dir):
    storage = JSONStorage("conv.json", storage_dir)
    value = {"version": 1, "conversations": {"example": {"text": "Fête à Liège — 音楽", "items": [1]}}}
    storage.write(value)
    assert "Fête à Liège — 音楽" in storage.path.read_text(encoding="utf-8")
    assert '\n  "version"' in storage.path.read_text(encoding="utf-8")
    value["conversations"]["example"]["items"].append(2)
    loaded = storage.read()
    loaded["conversations"]["example"]["items"].append(3)
    assert storage.read()["conversations"]["example"]["items"] == [1]


def test_atomic_replacement_uses_same_directory_and_syncs_first(monkeypatch, storage_dir):
    storage = JSONStorage("logs.json", storage_dir)
    storage.read()
    old = storage.path.read_bytes()
    events = []
    real_sync, real_replace = module.os.fsync, module.os.replace

    def sync(fd):
        events.append("sync")
        real_sync(fd)

    def replace(source, destination):
        assert Path(source).parent == Path(destination).parent == storage_dir
        assert storage.path.read_bytes() == old
        assert json.loads(Path(source).read_text(encoding="utf-8"))["events"] == [{"example": 1}]
        assert events == ["sync"]
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "fsync", sync)
    monkeypatch.setattr(module.os, "replace", replace)
    storage.write({"version": 1, "events": [{"example": 1}]})
    assert events == ["sync", "replace"]
    assert list(storage_dir.iterdir()) == [storage.path]


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_interrupted_write_preserves_original_and_cleans_temp(failure_point, monkeypatch, storage_dir):
    storage = JSONStorage("logs.json", storage_dir)
    storage.read()
    before = storage.path.read_bytes()

    def fail(*args):
        raise PermissionError("private-filesystem-details")

    monkeypatch.setattr(module.os, failure_point, fail)
    with pytest.raises(StorageIOError) as failure:
        storage.write({"version": 1, "events": [{"changed": True}]})
    assert "private-filesystem-details" not in str(failure.value)
    assert storage.path.read_bytes() == before
    assert list(storage_dir.iterdir()) == [storage.path]


@pytest.mark.parametrize("raw", ["", "  \n", "{broken", "[]", '{"version":2,"events":[]}',
                                 '{"version":true,"events":[]}', '{"version":1,"events":{}}',
                                 '{"version":1,"events":[],"events":[]}',
                                 '{"version":1,"events":[NaN]}', '{"version":1,"events":[1e999]}'])
def test_invalid_existing_file_never_overwritten(raw, storage_dir):
    storage_dir.mkdir()
    path = storage_dir / "logs.json"
    path.write_text(raw, encoding="utf-8")
    storage = JSONStorage("logs.json", storage_dir)
    for operation in [storage.read, lambda: storage.write({"version": 1, "events": []}),
                      lambda: storage.update(lambda data: data["events"].clear())]:
        with pytest.raises(StorageFormatError):
            operation()
        assert path.read_text(encoding="utf-8") == raw


def test_invalid_utf8_is_not_replaced(storage_dir):
    storage_dir.mkdir()
    path = storage_dir / "logs.json"
    path.write_bytes(b"\xff\xfe\xff")
    with pytest.raises(StorageFormatError):
        JSONStorage("logs.json", storage_dir).read()
    assert path.read_bytes() == b"\xff\xfe\xff"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), (1, 2), {1: "non-string key"}])
def test_non_json_values_rejected_without_write(value, storage_dir):
    storage = JSONStorage("logs.json", storage_dir)
    storage.read()
    before = storage.path.read_bytes()
    with pytest.raises(ValueError):
        storage.write({"version": 1, "events": [value]})
    assert storage.path.read_bytes() == before


def test_mutation_failure_and_cyclic_updates_leave_file_unchanged(storage_dir):
    storage = JSONStorage("logs.json", storage_dir)
    storage.read()
    before = storage.path.read_bytes()

    def mutate_then_fail(data):
        data["events"].append({"changed": True})
        raise RuntimeError("Stop")

    with pytest.raises(RuntimeError):
        storage.update(mutate_then_fail)
    with pytest.raises(ValueError):
        storage.update(lambda data: data["events"].append(data))
    assert storage.path.read_bytes() == before


def test_update_returns_independent_result(storage_dir):
    storage = JSONStorage("logs.json", storage_dir)

    def mutate(data):
        data["events"].append({"items": [1]})
        return data["events"][0]

    result = storage.update(mutate)
    result["items"].append(2)
    assert storage.read()["events"] == [{"items": [1]}]


def test_concurrent_read_modify_write_across_instances_and_path_aliases(storage_dir):
    first = JSONStorage("conv.json", storage_dir)
    second = JSONStorage("conv.json", storage_dir / ".." / "logs")
    first.write({"version": 1, "conversations": {"counter": 0}})

    def increment(index):
        def mutate(data):
            value = data["conversations"]["counter"]
            time.sleep(0.001)
            data["conversations"]["counter"] = value + 1
        (first if index % 2 else second).update(mutate)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment, range(32)))
    assert first.read()["conversations"]["counter"] == 32


@pytest.mark.parametrize("name", ["../.env", "..\\logs.json", "/tmp/logs.json", "other.json", None, []])
def test_unregistered_storage_filenames_rejected(name, storage_dir):
    with pytest.raises(ValueError):
        JSONStorage(name, storage_dir)
    assert not storage_dir.exists()


def test_symbolic_link_storage_rejected(monkeypatch, storage_dir):
    storage = JSONStorage("logs.json", storage_dir)
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == storage.path)
    with pytest.raises(StorageIOError, match="symbolic"):
        storage.read()
