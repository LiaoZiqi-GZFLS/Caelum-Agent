"""Tests for security guard."""

from agent.config import SecurityConfig
from agent.security import SecurityGuard


def test_auto_execute_read():
    guard = SecurityGuard(SecurityConfig())
    approval = guard.check("read", {"server": "filesystem", "tool": "read_file"})
    assert approval.allowed


def test_risky_requires_confirmation():
    guard = SecurityGuard(SecurityConfig())
    approval = guard.check("write_risky", {"server": "windows", "tool": "Click"})
    assert not approval.allowed
    assert "confirm" in approval.reason.lower()


def test_destructive_requires_approval():
    guard = SecurityGuard(SecurityConfig())
    approval = guard.check("destructive", {"server": "windows", "tool": "PowerShell"})
    assert not approval.allowed


def test_classify_tool_call():
    guard = SecurityGuard(SecurityConfig())
    assert guard.classify_tool_call("windows", "Snapshot") == "read"
    assert guard.classify_tool_call("windows", "Click") == "write_risky"
    assert guard.classify_tool_call("windows", "PowerShell") == "destructive"
