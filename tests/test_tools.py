"""Tests for CodeRunner sandbox."""

from __future__ import annotations

import subprocess

import pytest

from agent.tools import (
    CodeRunner,
    RestrictedCodeRunner,
    UnsafeCodeError,
    register_all,
    run_code,
)
from tests.fakes import FakeMCP


@pytest.fixture
def runner():
    return CodeRunner()


def test_allowed_math_code(runner):
    result = runner.run("import math\nprint(math.sqrt(16))")
    assert "4.0" in result


def test_allowed_string_module(runner):
    result = runner.run("import string\nprint(string.ascii_lowercase[:3])")
    assert "abc" in result


def test_disallowed_import(runner):
    result = runner.run("import os\nprint(os.getcwd())")
    assert result.startswith("[error]")
    assert "Import not allowed" in result


def test_disallowed_open(runner):
    result = runner.run("open('file.txt')")
    assert result.startswith("[error]")


def test_disallowed_subprocess_via_string(runner):
    result = runner.run("import subprocess\nsubprocess.run(['whoami'])")
    assert result.startswith("[error]")


def test_ast_validator_blocks_import(runner):
    with pytest.raises(UnsafeCodeError):
        runner._validate_ast("import os")


def test_ast_validator_allows_math(runner):
    runner._validate_ast("import math\nprint(math.pi)")


def test_empty_output_returns_ok(runner):
    result = runner.run("x = 1 + 1")
    assert result == "[ok] No output."


def test_code_too_long(runner):
    long_code = "x = 1\n" * 2000
    result = runner.run(long_code)
    assert result.startswith("[error]")
    assert "too long" in result


def test_timeout_catches_infinite_loop():
    # A 1s-timeout runner keeps the test fast; waiting out the production
    # default (10s) is not what this test needs to prove.
    runner = CodeRunner(timeout_seconds=1)
    result = runner.run("while True: pass", language="python")
    assert result.startswith("[error]")
    assert "timed out" in result


def test_backwards_compatible_run_code():
    result = run_code("print('hello')")
    assert "hello" in result


def test_sanitizes_shebang(runner):
    result = runner.run("#!/usr/bin/env python\nprint(1)")
    assert "1" in result


def test_unsupported_language(runner):
    result = runner.run("print(1)", language="ruby")
    assert result.startswith("[error]")
    assert "ruby" in result


def test_restricted_builtins_at_runtime(runner):
    # Direct call to eval passes AST validation but is blocked at runtime.
    result = runner.run("eval('1 + 1')")
    assert "[stderr]" in result or result.startswith("[error]")


def test_javascript_returns_error_without_node(monkeypatch):
    runner = CodeRunner(allow_javascript=True)
    monkeypatch.setattr("shutil.which", lambda _: None)
    result = runner.run("console.log(1)", language="javascript")
    assert result.startswith("[error]")
    assert "Node.js" in result


def test_restricted_code_runner_blocks_disallowed_imports():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    result = runner.run("import os\nprint(os.getcwd())")
    assert "[error]" in result


def test_restricted_code_runner_allows_whitelisted_imports():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    result = runner.run("import math\nprint(math.sqrt(16))")
    assert "4.0" in result


def test_run_code_uses_restricted_runner():
    from agent.tools import run_code
    result = run_code("import os\nprint('hi')")
    assert "[error]" in result


def test_restricted_runner_no_original_import_leak():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    result = runner.run("print(_ORIGINAL_IMPORT('os').getcwd())")
    assert "[error]" in result or "[stderr]" in result


def test_restricted_runner_allows_json():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    result = runner.run("import json\nprint(json.dumps({'a': 1}))")
    assert '{"a": 1}' in result


def test_restricted_runner_allows_datetime():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    result = runner.run("import datetime\nprint(datetime.MINYEAR)")
    assert "1" in result


def test_restricted_runner_no_globals_leak():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    code = "g = __import__.__globals__\nprint('builtins' in g or 'sys' in g or 'importlib' in g)"
    result = runner.run(code)
    assert "True" not in result


def test_restricted_runner_no_help_escape():
    from agent.tools import RestrictedCodeRunner
    runner = RestrictedCodeRunner()
    code = "g = help.__class__.__call__.__globals__\nprint('os' in g)"
    result = runner.run(code)
    assert "True" not in result


# ---------------------------------------------------------------------------
# Working directory (cwd) handling
# ---------------------------------------------------------------------------

def test_run_passes_configured_cwd_to_subprocess(tmp_path, monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    CodeRunner(cwd=str(tmp_path)).run("print('hi')")
    assert captured["cwd"] == str(tmp_path)


def test_run_creates_cwd_when_missing(tmp_path):
    target = tmp_path / "deep" / "cache"
    result = CodeRunner(cwd=str(target)).run("print('hi')")
    assert target.is_dir()
    assert "hi" in result


def test_run_without_cwd_leaves_subprocess_cwd_unset(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    CodeRunner().run("print('hi')")
    assert captured.get("cwd") is None


def test_register_all_wires_code_cwd(tmp_path, monkeypatch):
    from agent.tools import register_all
    from tests.fakes import FakeMCP

    class _CaptureLLM:
        def __init__(self):
            self.local = {}

        def register_function_tools(self, tools):
            pass

        def register_local_function(self, name, fn, **kwargs):
            self.local[name] = fn

    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    llm = _CaptureLLM()
    register_all(llm, FakeMCP(), code_cwd=str(tmp_path))
    llm.local["CodeRunner"](code="print('hi')", language="python")
    assert captured["cwd"] == str(tmp_path)


# ---------------------------------------------------------------------------
# JavaScript gating (--yes / --yes-all only)
# ---------------------------------------------------------------------------


def test_javascript_blocked_by_default():
    runner = RestrictedCodeRunner()
    result = runner.run("console.log(1)", language="javascript")
    assert result.startswith("[error]")
    assert "--yes" in result


def test_javascript_blocked_error_mentions_interactive():
    runner = RestrictedCodeRunner(allow_javascript=False)
    result = runner.run("console.log(1)", language="javascript")
    assert "interactive" in result.lower() or "--yes" in result


def test_javascript_gate_passes_when_allowed(tmp_path):
    # With the gate open, execution proceeds to the Node lookup. On machines
    # without Node this returns the not-found error; either way it must NOT be
    # the gating error.
    runner = RestrictedCodeRunner(cwd=str(tmp_path), allow_javascript=True)
    result = runner.run("console.log('hi')", language="javascript")
    assert "--yes" not in result
    assert "requires Node.js" in result or "hi" in result


def test_python_unaffected_by_javascript_gate(tmp_path):
    runner = RestrictedCodeRunner(cwd=str(tmp_path))
    result = runner.run("print(2 + 2)", language="python")
    assert "4" in result


def test_register_all_wires_javascript_gate():
    class _LLM:
        def __init__(self):
            self.local = {}

        def register_function_tools(self, tools):
            pass

        def register_local_function(self, name, handler, schema, description):
            self.local[name] = handler

    llm = _LLM()
    register_all(llm, FakeMCP(), allow_javascript=True)
    assert llm.local["CodeRunner"].__self__.allow_javascript is True

    llm2 = _LLM()
    register_all(llm2, FakeMCP())
    assert llm2.local["CodeRunner"].__self__.allow_javascript is False


def test_build_mcp_tools_warns_windows_label_freshness():
    from agent.tools import build_mcp_tools

    mcp = FakeMCP([
        {"server": "windows", "name": "Type", "description": "Type text.", "schema": {}},
        {"server": "windows", "name": "Click", "description": "Click.", "schema": {}},
        {"server": "windows", "name": "Snapshot", "description": "Snapshot.", "schema": {}},
        {"server": "playwright", "name": "browser_click", "description": "Click.", "schema": {}},
    ])
    tools = {t["function"]["name"]: t["function"]["description"] for t in build_mcp_tools(mcp)}

    # Positional windows tools get the label-expiry warning.
    assert "invalidat" in tools["windows__Type"].lower()
    assert "invalidat" in tools["windows__Click"].lower()
    # Snapshot itself and non-windows tools are untouched.
    assert "invalidat" not in tools["windows__Snapshot"].lower()
    assert "invalidat" not in tools["playwright__browser_click"].lower()


# ---------------------------------------------------------------------------
# build_mcp_tools — edge cases
# ---------------------------------------------------------------------------

def test_build_mcp_tools_empty():
    from agent.tools import build_mcp_tools

    mcp = FakeMCP([])
    assert build_mcp_tools(mcp) == []

def test_build_mcp_tools_preserves_schema():
    from agent.tools import build_mcp_tools

    mcp = FakeMCP([
        {"server": "w", "name": "X", "description": "d",
         "schema": {"type": "object", "properties": {"x": {"type": "number"}}}},
    ])
    tools = build_mcp_tools(mcp)
    assert tools[0]["function"]["parameters"] == {
        "type": "object", "properties": {"x": {"type": "number"}},
    }


# ---------------------------------------------------------------------------
# CodeRunner — AST edge cases
# ---------------------------------------------------------------------------

def test_ast_allowed_multiline(runner):
    result = runner.run("x = 1\ny = 2\nprint(x + y)")
    assert "3" in result

def test_ast_rejects_import_as(runner):
    result = runner.run("import os as _os\nprint(_os.getcwd())")
    assert result.startswith("[error]")

def test_ast_rejects_from_import(runner):
    result = runner.run("from os import getcwd\nprint(getcwd())")
    assert result.startswith("[error]")

def test_ast_allows_safe_builtins(runner):
    result = runner.run("print(len([1, 2, 3]))")
    assert "3" in result

def test_ast_rejects_exec(runner):
    result = runner.run("exec('print(1)')")
    assert result.startswith("[error]")

def test_ast_rejects_eval(runner):
    result = runner.run("eval('1 + 1')")
    assert result.startswith("[error]")


# ---------------------------------------------------------------------------
# Schema structure validation
# ---------------------------------------------------------------------------

def test_all_schemas_are_objects():
    from agent.tools import (
        COMPLETE_TASK_SCHEMA,
        DESKTOP_INTERACT_SCHEMA,
        NEARBY_LABELS_SCHEMA,
        PREVIEW_POINTS_SCHEMA,
        UPGRADE_VISION_SCHEMA,
        ZOOM_REGION_SCHEMA,
    )
    schemas = [
        DESKTOP_INTERACT_SCHEMA,
        NEARBY_LABELS_SCHEMA,
        ZOOM_REGION_SCHEMA,
        UPGRADE_VISION_SCHEMA,
        PREVIEW_POINTS_SCHEMA,
        COMPLETE_TASK_SCHEMA,
    ]
    for s in schemas:
        assert s["type"] == "object"
        assert "properties" in s

def test_desktop_interact_schema_enforces_label_required():
    from agent.tools import DESKTOP_INTERACT_SCHEMA

    assert "label" in DESKTOP_INTERACT_SCHEMA["required"]
    assert "label" in DESKTOP_INTERACT_SCHEMA["properties"]
    assert DESKTOP_INTERACT_SCHEMA["properties"]["label"]["type"] == "integer"

def test_desktop_interact_schema_actions():
    from agent.tools import DESKTOP_INTERACT_SCHEMA

    actions = DESKTOP_INTERACT_SCHEMA["properties"]["action"]["enum"]
    assert "click" in actions
    assert "double_click" in actions
    assert "type" in actions
    assert "scroll_down" in actions
    assert "scroll_up" in actions
