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
    # Browser mutating actions should be risky.
    assert guard.classify_tool_call("playwright", "browser_type") == "write_risky"
    assert guard.classify_tool_call("playwright", "browser_fill_form") == "write_risky"
    assert guard.classify_tool_call("playwright", "browser_evaluate") == "write_risky"
    assert guard.classify_tool_call("playwright", "browser_route") == "write_risky"
    # Windows MCP specific tools.
    assert guard.classify_tool_call("windows", "App") == "write_risky"
    assert guard.classify_tool_call("windows", "MultiEdit") == "write_risky"
    assert guard.classify_tool_call("windows", "FileSystem") == "write_risky"


def test_auto_approve_write_risky():
    """--yes auto-approves write_risky even without a confirmation callback."""
    guard = SecurityGuard(SecurityConfig(), auto_approve=True)
    approval = guard.check("write_risky", {"server": "windows", "tool": "Click"})
    assert approval.allowed
    assert "auto-approved" in approval.reason


def test_auto_approve_does_not_cover_destructive():
    """--yes must NOT auto-approve destructive actions."""
    guard = SecurityGuard(
        SecurityConfig(), auto_approve=True, auto_approve_destructive=False
    )
    approval = guard.check("destructive", {"server": "windows", "tool": "PowerShell"})
    assert not approval.allowed


def test_auto_approve_destructive_skips_typed_confirmation(monkeypatch):
    """--yes-all approves destructive actions without invoking input()."""

    def _no_input(_prompt: str = "") -> str:
        raise AssertionError("input() must not be called when auto-approving destructive")

    monkeypatch.setattr("builtins.input", _no_input)
    guard = SecurityGuard(SecurityConfig(), auto_approve_destructive=True)
    approval = guard.check("destructive", {"server": "windows", "tool": "PowerShell"})
    assert approval.allowed
    assert "auto-approved" in approval.reason
