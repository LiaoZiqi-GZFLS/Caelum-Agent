"""Tests for the flight-recorder history archive (agent/history_archive.py)."""

from __future__ import annotations

import json
from pathlib import Path

from agent.history_archive import HistoryArchiver


def _read_archive(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_archive_writes_metadata_and_messages(tmp_path: Path) -> None:
    archiver = HistoryArchiver(tmp_path / "archives")
    history = [
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": "open notepad"},
    ]

    path = archiver.archive(
        task_id="task-1", instruction="open notepad", outcome="done", history=history
    )

    assert path.exists()
    assert path.parent == tmp_path / "archives"
    records = _read_archive(path)
    assert records[0]["type"] == "metadata"
    assert records[0]["task_id"] == "task-1"
    assert records[0]["outcome"] == "done"
    assert "archived_at" in records[0]
    assert records[1:] == history


def test_archive_strips_base64_images(tmp_path: Path) -> None:
    archiver = HistoryArchiver(tmp_path / "archives")
    history = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "screen"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ],
    }]

    path = archiver.archive(task_id=None, instruction="t", outcome="done", history=history)

    records = _read_archive(path)
    parts = records[1]["content"]
    assert parts[0] == {"type": "text", "text": "screen"}
    assert "base64" not in json.dumps(parts)
    assert "omitted" in json.dumps(parts)


def test_archive_redacts_tool_call_arguments(tmp_path: Path) -> None:
    archiver = HistoryArchiver(
        tmp_path / "archives", sensitive_keys=frozenset({"text", "password"})
    )
    history = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "c1",
            "function": {
                "name": "windows__Type",
                "arguments": json.dumps({"label": "3", "text": "hunter2"}),
            },
        }],
    }]

    path = archiver.archive(task_id="t", instruction="t", outcome="done", history=history)

    raw = path.read_text(encoding="utf-8")
    assert "hunter2" not in raw
    args = json.loads(_read_archive(path)[1]["tool_calls"][0]["function"]["arguments"])
    assert args == {"label": "3", "text": "***"}


def test_archive_never_raises_on_bad_history(tmp_path: Path) -> None:
    archiver = HistoryArchiver(tmp_path / "archives")
    # Non-serializable content must not kill the agent; returns None.
    result = archiver.archive(
        task_id="t", instruction="t", outcome="done",
        history=[{"role": "user", "content": object()}],
    )
    assert result is None
