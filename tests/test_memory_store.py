from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from harness.memory import MemoryStore
from harness.storage.sqlite import Conflict, NotFound, RunStore


@pytest.fixture
def stores():
    with TemporaryDirectory() as directory:
        runs = RunStore(Path(directory))
        first = runs.create("用户请求", None, None)
        second = runs.create("其他会话", None, None)
        yield runs, MemoryStore(runs), first, second


def test_session_and_personal_scope_and_chinese_search(stores):
    _, memories, first, second = stores
    session = memories.remember(first["session_id"], "style", "喜欢简洁的网页设计")
    shared = memories.remember(first["session_id"], "language", "默认使用中文", scope="global")
    assert memories.search(first["session_id"], "网页")[0]["id"] == session["id"]
    assert memories.search(first["session_id"], "网页设计")[0]["id"] == session["id"]
    assert memories.search(second["session_id"], "网页") == []
    assert memories.search(second["session_id"], "中文") == []
    assert memories.search(second["session_id"], "中文", include_global=True)[0]["id"] == shared["id"]
    with pytest.raises(NotFound):
        memories.get(second["session_id"], session["id"], include_global=True)


def test_version_evidence_forget_and_stale_extraction(stores):
    runs, memories, first, _ = stores
    session_id = first["session_id"]
    original = memories.remember(session_id, "style", "蓝色", source_run_id=first["id"])
    updated = memories.remember(session_id, "style", "绿色", expected_version=1)
    assert updated["id"] == original["id"]
    assert updated["version"] == 2
    with pytest.raises(Conflict):
        memories.remember(session_id, "style", "黄色", expected_version=1)
    assert memories.source(session_id, original["id"])[0]["run_id"] == first["id"]
    candidate = [{"key": "style", "content": "蓝色", "kind": "preference"}]
    with runs.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        assert not memories.apply_extracted(db, session_id, first["id"], first["created_at"], candidate)
    assert memories.get(session_id, original["id"])["content"] == "绿色"
    memories.forget(session_id, original["id"])
    with pytest.raises(NotFound):
        memories.get(session_id, original["id"])
    with runs.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        assert not memories.apply_extracted(db, session_id, first["id"], first["created_at"], candidate)
        assert db.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0] == 0
    restored = memories.remember(session_id, "style", "紫色")
    assert restored["id"] != original["id"]


@pytest.mark.parametrize("key,content", [
    ("api_key", "example-value"),
    ("account", "密码是 123456"),
    ("account", "Authorization: Bearer abcdef"),
    ("service", "https://user:password@example.com"),
    ("service", "sk-abcdefghijklmnop"),
    ("service", "ghp_abcdefghijklmnop"),
    ("service", "-----BEGIN PRIVATE KEY-----"),
])
def test_credentials_rejected_from_explicit_and_extracted_memory(stores, key, content):
    runs, memories, first, _ = stores
    with pytest.raises(ValueError, match="Credentials"):
        memories.remember(first["session_id"], key, content)
    with runs.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        assert memories.apply_extracted(db, first["session_id"], first["id"], first["created_at"],
                                        [{"key": key, "content": content}]) == []
    assert memories.search(first["session_id"]) == []
