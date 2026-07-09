"""Agent state machine with async transitions and event emission."""

from __future__ import annotations

from eventbus import EventBus
from eventbus.events import AgentStateChanged


VALID_TRANSITIONS: dict[str, set[str]] = {
    "IDLE": {"PLANNING"},
    "PLANNING": {"IDLE", "EXECUTING", "WAITING_HUMAN", "REFLECT", "ERROR", "STUCK"},
    "EXECUTING": {"IDLE", "VERIFYING", "WAITING_HUMAN", "REFLECT", "COMPLETED", "ERROR", "STUCK"},
    "VERIFYING": {"IDLE", "PLANNING", "EXECUTING", "COMPLETED", "REFLECT", "ERROR", "STUCK"},
    "WAITING_HUMAN": {"IDLE", "PLANNING", "EXECUTING", "REFLECT", "ERROR"},
    "REFLECT": {"IDLE", "PLANNING", "WAITING_HUMAN", "ERROR", "STUCK"},
    "COMPLETED": {"IDLE"},
    "ERROR": {"IDLE"},
    "STUCK": {"IDLE", "REFLECT"},
}


class AgentStateMachine:
    def __init__(self, eventbus: EventBus, initial: str = "IDLE") -> None:
        self._eventbus = eventbus
        self._state = initial

    @property
    def current_state(self) -> str:
        return self._state

    def can_transition(self, new_state: str) -> bool:
        return new_state in VALID_TRANSITIONS.get(self._state, set())

    async def transition(self, new_state: str, task_id: str | None = None) -> bool:
        if new_state == self._state:
            return True
        if not self.can_transition(new_state):
            return False
        old = self._state
        self._state = new_state
        await self._eventbus.emit(
            AgentStateChanged(old_state=old, new_state=new_state, task_id=task_id)
        )
        return True

    def is_terminal(self) -> bool:
        return self._state in {"COMPLETED", "ERROR", "STUCK"}
