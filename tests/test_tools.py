"""Tests for CodeRunner sandbox."""

from __future__ import annotations

import pytest

from agent.tools import CodeRunner, UnsafeCodeError, run_code


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


def test_timeout_catches_infinite_loop(runner):
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


def test_javascript_returns_error_without_node(runner, monkeypatch):
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
