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


# ---------------------------------------------------------------------------
# _summarize
# ---------------------------------------------------------------------------

def test_summarize_formats_action():
    summary = SecurityGuard._summarize(
        {"server": "windows", "tool": "Click", "args": {"label": 5}}
    )
    assert summary == "windows/Click(label=5)"

def test_summarize_empty_args():
    summary = SecurityGuard._summarize({"server": "playwright", "tool": "browser_navigate"})
    assert "playwright/browser_navigate" in summary
    assert summary.endswith("()")

def test_summarize_missing_keys():
    summary = SecurityGuard._summarize({})
    assert "unknown/unknown()" == summary

def test_summarize_special_chars_in_args():
    summary = SecurityGuard._summarize(
        {"server": "w", "tool": "t", "args": {"text": "hello world", "n": 1}}
    )
    assert "w/t(text='hello world', n=1)" == summary


# ---------------------------------------------------------------------------
# _request_confirmation — no callback
# ---------------------------------------------------------------------------

def test_no_callback_handler():
    guard = SecurityGuard(SecurityConfig(), confirm_callback=None)
    approval = guard.check("write_risky", {"server": "windows", "tool": "Click"})
    assert not approval.allowed
    assert "no confirmation handler" in approval.reason.lower()


# ---------------------------------------------------------------------------
# _request_confirmation — callback deny / allow
# ---------------------------------------------------------------------------

def test_human_denied_via_callback():
    guard = SecurityGuard(
        SecurityConfig(),
        confirm_callback=lambda summary, action: False,
    )
    approval = guard.check("write_risky", {"server": "windows", "tool": "Type"})
    assert not approval.allowed
    assert "human-denied" in approval.reason

def test_human_confirmed_via_callback():
    guard = SecurityGuard(
        SecurityConfig(),
        confirm_callback=lambda summary, action: True,
    )
    approval = guard.check("write_risky", {"server": "windows", "tool": "Type"})
    assert approval.allowed
    assert "human-confirmed" in approval.reason


# ---------------------------------------------------------------------------
# _typed_confirmation
# ---------------------------------------------------------------------------

def test_typed_confirmation_success(monkeypatch):
    guard = SecurityGuard(
        SecurityConfig(),
        confirm_callback=lambda summary, action: True,
    )
    monkeypatch.setattr("builtins.input", lambda _: "w/t(k=1)")
    assert guard._typed_confirmation("w/t(k=1)") is True

def test_typed_confirmation_mismatch(monkeypatch):
    guard = SecurityGuard(
        SecurityConfig(),
        confirm_callback=lambda summary, action: True,
    )
    monkeypatch.setattr("builtins.input", lambda _: "wrong summary")
    assert guard._typed_confirmation("w/t(k=1)") is False

def test_typed_confirmation_eof(monkeypatch):
    guard = SecurityGuard(
        SecurityConfig(),
        confirm_callback=lambda summary, action: True,
    )
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
    assert guard._typed_confirmation("w/t(k=1)") is False

def test_typed_confirmation_callback_denied():
    guard = SecurityGuard(
        SecurityConfig(),
        confirm_callback=lambda summary, action: False,
    )
    assert guard._typed_confirmation("w/t(k=1)") is False

def test_typed_confirmation_no_callback():
    guard = SecurityGuard(SecurityConfig(), confirm_callback=None)
    assert guard._typed_confirmation("w/t(k=1)") is False


# ---------------------------------------------------------------------------
# check — edge cases
# ---------------------------------------------------------------------------

def test_destructive_allows_when_config_disabled():
    cfg = SecurityConfig(destructive_requires_approval=False)
    guard = SecurityGuard(cfg)
    approval = guard.check("destructive", {"server": "windows", "tool": "PowerShell"})
    assert approval.allowed
    assert "default allow" in approval.reason

def test_default_allow_unknown_level():
    guard = SecurityGuard(SecurityConfig())
    approval = guard.check("custom_unknown", {"server": "x", "tool": "y"})
    assert approval.allowed
    assert "default allow" in approval.reason


# ---------------------------------------------------------------------------
# classify_tool_call — edge cases
# ---------------------------------------------------------------------------

def test_classify_case_insensitive():
    guard = SecurityGuard(SecurityConfig())
    assert guard.classify_tool_call("Windows", "DELETE") == "destructive"
    assert guard.classify_tool_call("Windows", "CLICK") == "write_risky"

def test_classify_empty_names():
    guard = SecurityGuard(SecurityConfig())
    assert guard.classify_tool_call("", "") == "read"

def test_classify_partial_match():
    guard = SecurityGuard(SecurityConfig())
    assert guard.classify_tool_call("windows", "DeleteFile") == "destructive"
    assert guard.classify_tool_call("windows", "RegistryKey") == "destructive"
