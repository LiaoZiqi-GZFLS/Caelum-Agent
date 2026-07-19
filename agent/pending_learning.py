"""Startup settlement of interrupted-task learning records.

When a task is interrupted by the kill switch or the API circuit breaker,
the orchestrator queues its trajectory into the ``pending_learning`` SQLite
table (``MemoryStore.add_pending_learning``). On the NEXT startup the
``LearningSettler`` reviews each queued trajectory with the LLM, judges how
complete it was, and settles it one of two ways:

- substantively completed -> ``SkillLearner.learn`` (success memory, the
  normal SKILL.md generation/merge path)
- not completed -> ``ReflectionEngine.record`` (failure reflection)

Settlement is best-effort and never blocks startup (the orchestrator
schedules it as a background task). A judging failure keeps the row for the
next startup; after ``MAX_ATTEMPTS`` failed attempts the row is settled as a
plain failure reflection so it can never become a zombie.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("caelum.pending_learning")

_MAX_TRACES_CHARS = 4000

_JUDGE_INSTRUCTION = (
    "You are reviewing a desktop-automation task that was INTERRUPTED before "
    "the agent declared it finished. Judge whether the task was "
    "substantively COMPLETED despite the interruption.\n\n"
    "Reply with a JSON object only (no prose, no code fences):\n"
    '{"completed": true|false, "summary": "<one sentence: what the trajectory achieved>", '
    '"lesson": "<one sentence: the reusable takeaway>"}\n\n'
    "completed=true means a reasonable user would consider the instruction "
    "fulfilled by the actions taken; partial progress is NOT completed."
)


def _parse_verdict(content: str) -> dict[str, Any]:
    """Parse the judge's JSON verdict; tolerates code fences / surrounding text."""
    text = content.strip()
    if text.startswith("```"):
        # Strip ```json ... ``` fences.
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        verdict = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"judge verdict is not JSON: {content[:120]!r}")
        verdict = json.loads(text[start : end + 1])
    if not isinstance(verdict, dict) or "completed" not in verdict:
        raise ValueError(f"judge verdict missing 'completed': {content[:120]!r}")
    return verdict


class LearningSettler:
    """Settles queued interrupted-task records into skills or reflections."""

    MAX_ATTEMPTS = 3

    def __init__(
        self,
        memory: Any,
        llm: Any,
        skill_learner: Any,
        reflection: Any,
    ) -> None:
        self.memory = memory
        self.llm = llm
        self.skill_learner = skill_learner
        self.reflection = reflection

    async def settle_all(self) -> int:
        """Settle every queued record; returns how many were settled."""
        rows = self.memory.list_pending_learning()
        if not rows:
            return 0
        logger.info("Settling %d interrupted task(s) from previous runs", len(rows))
        settled = 0
        for row in rows:
            if await self._settle_one(row):
                settled += 1
        return settled

    async def _settle_one(self, row: dict[str, Any]) -> bool:
        """True when the row was settled (deleted from the queue)."""
        try:
            verdict = await self._judge(row)
        except Exception as exc:
            logger.warning(
                "Learning settlement judge failed for %r: %s",
                row["instruction"][:80],
                exc,
            )
            attempts = self.memory.bump_pending_learning_attempts(row["id"])
            if attempts >= self.MAX_ATTEMPTS:
                await self._fallback_reflection(row, attempts)
                self.memory.delete_pending_learning(row["id"])
                return True
            return False
        if verdict.get("completed"):
            await self.skill_learner.learn(row["instruction"], row["traces"])
            logger.info(
                "Interrupted task judged COMPLETED; learned skill: %s",
                row["instruction"][:80],
            )
        else:
            await self.reflection.record(
                task_summary=row["instruction"],
                failure_reason=(
                    f"Task interrupted ({row['reason']}); "
                    f"{verdict.get('summary', 'judged incomplete')}"
                ),
                fix_action=verdict.get("lesson") or "Retry the task if still needed.",
            )
            logger.info(
                "Interrupted task judged incomplete; recorded reflection: %s",
                row["instruction"][:80],
            )
        self.memory.delete_pending_learning(row["id"])
        return True

    async def _fallback_reflection(self, row: dict[str, Any], attempts: int) -> None:
        """Give up judging after MAX_ATTEMPTS: settle as a plain reflection."""
        try:
            await self.reflection.record(
                task_summary=row["instruction"],
                failure_reason=(
                    f"Task interrupted ({row['reason']}); settlement judge "
                    f"unavailable after {attempts} attempts"
                ),
                fix_action="Review data/archives for the trajectory and retry if needed.",
            )
        except Exception as exc:  # pragma: no cover - last-ditch containment
            logger.warning("Fallback reflection failed for %r: %s", row["instruction"][:80], exc)

    async def _judge(self, row: dict[str, Any]) -> dict[str, Any]:
        traces_text = "\n".join(f"- {t}" for t in row["traces"]) or "(no actions recorded)"
        if len(traces_text) > _MAX_TRACES_CHARS:
            traces_text = traces_text[:_MAX_TRACES_CHARS] + "\n- ...(truncated)"
        reason = (
            "the user pressed the kill switch"
            if row["reason"] == "kill_switch"
            else "the LLM API failed repeatedly (circuit breaker)"
        )
        messages = [
            {"role": "system", "content": _JUDGE_INSTRUCTION},
            {
                "role": "user",
                "content": (
                    f"Task instruction: {row['instruction']}\n"
                    f"Interrupted because: {reason}\n\n"
                    f"Actions taken before the interruption:\n{traces_text}"
                ),
            },
        ]
        completion = await self.llm.chat(messages, tool_choice="none")
        content = completion.choices[0].message.content or ""
        return _parse_verdict(content)
