"""Tests for the model-managed task list (agent/task_list.py)."""

from __future__ import annotations

from typing import Any

import pytest

from agent.task_list import (
    UPDATE_TASK_LIST_SCHEMA,
    TaskList,
    make_update_task_list_handler,
    register_task_list,
)


def test_update_replaces_and_renders() -> None:
    tl = TaskList()
    tl.update([
        {"content": "open WPS", "status": "completed"},
        {"content": "write body", "status": "in_progress"},
        {"content": "save file", "status": "pending"},
    ])

    rendered = tl.render()
    assert "open WPS" in rendered and "write body" in rendered
    assert "[x]" in rendered  # completed marker
    assert "[>]" in rendered  # in_progress marker
    assert "[ ]" in rendered  # pending marker


def test_update_replaces_previous_list() -> None:
    tl = TaskList()
    tl.update([{"content": "old", "status": "pending"}])
    tl.update([{"content": "new", "status": "pending"}])

    assert len(tl.items) == 1
    assert tl.items[0]["content"] == "new"


def test_update_rejects_invalid_status() -> None:
    tl = TaskList()
    tl.update([{"content": "keep me", "status": "pending"}])

    with pytest.raises(ValueError, match="status"):
        tl.update([{"content": "bad", "status": "doing"}])

    assert tl.items[0]["content"] == "keep me"  # unchanged on failure


def test_update_rejects_multiple_in_progress() -> None:
    tl = TaskList()
    with pytest.raises(ValueError, match="one task"):
        tl.update([
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ])
    assert tl.items == []


def test_update_rejects_empty_content() -> None:
    tl = TaskList()
    with pytest.raises(ValueError, match="content"):
        tl.update([{"content": "  ", "status": "pending"}])


def test_all_completed_property() -> None:
    tl = TaskList()
    assert tl.all_completed is False  # empty list is not "all completed"
    tl.update([{"content": "a", "status": "completed"}])
    assert tl.all_completed is True


def test_handler_clears_when_all_completed() -> None:
    tl = TaskList()
    handler = make_update_task_list_handler(tl)

    result = handler(tasks=[
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "completed"},
    ])

    assert "cleared" in result
    assert tl.items == []


def test_handler_returns_rendered_list() -> None:
    tl = TaskList()
    handler = make_update_task_list_handler(tl)

    result = handler(tasks=[{"content": "write tests", "status": "in_progress"}])

    assert "write tests" in result
    assert "in_progress" in result


def test_handler_returns_error_on_invalid_input() -> None:
    tl = TaskList()
    handler = make_update_task_list_handler(tl)

    result = handler(tasks=[{"content": "x", "status": "nope"}])

    assert result.startswith("[error]")
    assert tl.items == []


def test_schema_shape() -> None:
    assert UPDATE_TASK_LIST_SCHEMA["required"] == ["tasks"]
    item = UPDATE_TASK_LIST_SCHEMA["properties"]["tasks"]["items"]
    assert item["properties"]["status"]["enum"] == [
        "pending", "in_progress", "completed",
    ]


class _RecordingLLM:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def register_local_function(
        self, name: str, handler: Any, schema: dict[str, Any], description: str
    ) -> None:
        self.registered[name] = {"handler": handler, "schema": schema}


def test_register_task_list() -> None:
    llm = _RecordingLLM()
    register_task_list(llm, TaskList())

    assert "UpdateTaskList" in llm.registered
    assert llm.registered["UpdateTaskList"]["schema"] is UPDATE_TASK_LIST_SCHEMA
