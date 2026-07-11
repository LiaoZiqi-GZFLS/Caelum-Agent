"""Model-managed task list for long-task coherence.

With the loop budget extended to 50 rounds, a plan made in round 3 is buried
under tens of thousands of tokens of screenshots and tool results by round 30.
The UpdateTaskList tool gives the model a persistent scratchpad: it rewrites
the whole list atomically (add/remove/reorder/restyle freely), and the
orchestrator re-injects a compact render into the history every loop so the
plan stays salient instead of drifting out of working context.

The list is self-clearing: when every item is marked completed the handler
empties it, and run_task always starts from a clean slate.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("caelum.task_list")

VALID_STATUSES = ("pending", "in_progress", "completed")

_MARKERS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}


class TaskList:
    """An atomic-replace task list with status validation."""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def update(self, tasks: list[dict[str, Any]]) -> None:
        """Replace the whole list; validates first so failures keep the old list."""
        cleaned: list[dict[str, str]] = []
        in_progress = 0
        for i, task in enumerate(tasks or [], start=1):
            content = str(task.get("content", "")).strip()
            status = str(task.get("status", "")).strip()
            if not content:
                raise ValueError(f"Task #{i} has no content.")
            if status not in VALID_STATUSES:
                raise ValueError(
                    f"Task #{i} has invalid status '{status}'; "
                    f"use one of {', '.join(VALID_STATUSES)}."
                )
            if status == "in_progress":
                in_progress += 1
            cleaned.append({"content": content, "status": status})
        if in_progress > 1:
            raise ValueError("Only one task may be in_progress at a time.")
        self.items = cleaned

    @property
    def all_completed(self) -> bool:
        return bool(self.items) and all(
            t["status"] == "completed" for t in self.items
        )

    def clear(self) -> None:
        self.items = []

    def render(self) -> str:
        lines = ["Task list:"]
        for i, task in enumerate(self.items, start=1):
            lines.append(
                f"  {_MARKERS[task['status']]} {i}. {task['content']} ({task['status']})"
            )
        return "\n".join(lines)


def make_update_task_list_handler(task_list: TaskList):
    """Build the UpdateTaskList tool handler (synchronous)."""

    def update_task_list(tasks: list[dict[str, Any]]) -> str:
        try:
            task_list.update(tasks)
        except Exception as exc:
            return f"[error] {exc}"
        if task_list.all_completed:
            task_list.clear()
            return "All tasks completed; task list cleared."
        if not task_list.items:
            return "Task list is now empty."
        return task_list.render()

    return update_task_list


UPDATE_TASK_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "description": (
                "The complete new task list, replacing the previous one. "
                "Add, remove, reorder, or restyle items freely."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "One concrete step of the plan.",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(VALID_STATUSES),
                        "description": "pending = not started, in_progress = current step (at most one), completed = done.",
                    },
                },
                "required": ["content", "status"],
            },
        }
    },
    "required": ["tasks"],
}


def register_task_list(llm: Any, task_list: TaskList) -> None:
    """Register the UpdateTaskList local function tool."""
    llm.register_local_function(
        "UpdateTaskList",
        make_update_task_list_handler(task_list),
        schema=UPDATE_TASK_LIST_SCHEMA,
        description=(
            "Maintain a task list to stay coherent on multi-step tasks (3+ "
            "steps). Submit the COMPLETE new list each call: it replaces the "
            "previous one, so you can add, remove, or reorder steps freely. "
            "Keep exactly one step in_progress. Mark steps completed as you "
            "finish them; the list clears itself when all steps are done. "
            "The current list is shown to you every round, so update it "
            "instead of re-planning from memory."
        ),
    )
