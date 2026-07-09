"""Tests for agent state machine."""

import pytest

from agent.state_machine import AgentStateMachine
from eventbus import EventBus


@pytest.fixture
def fsm():
    return AgentStateMachine(EventBus())


@pytest.mark.asyncio
async def test_valid_transition(fsm):
    ok = await fsm.transition("PLANNING")
    assert ok
    assert fsm.current_state == "PLANNING"


@pytest.mark.asyncio
async def test_invalid_transition(fsm):
    ok = await fsm.transition("EXECUTING")
    assert not ok
    assert fsm.current_state == "IDLE"


@pytest.mark.asyncio
async def test_planning_to_idle(fsm):
    await fsm.transition("PLANNING")
    ok = await fsm.transition("IDLE")
    assert ok
    assert fsm.current_state == "IDLE"


@pytest.mark.asyncio
async def test_terminal_state(fsm):
    await fsm.transition("PLANNING")
    await fsm.transition("EXECUTING")
    await fsm.transition("VERIFYING")
    await fsm.transition("COMPLETED")
    assert fsm.is_terminal()
