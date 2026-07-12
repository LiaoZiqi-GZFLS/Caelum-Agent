"""Tool definitions: MCP tool mapping and local CodeRunner.

CodeRunner executes Python snippets in a subprocess sandbox. The snippet is
first checked by an AST validator that limits imports and disallows dangerous
built-ins. Execution is bounded by a timeout and returns stdout/stderr.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp_client import MCPMultiplexer


logger = logging.getLogger("caelum.tools")


DESKTOP_INTERACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "A short, concrete VISUAL description of the element to interact "
                "with (e.g. 'the red Send button right of the message input', "
                "'the search box at the top'). This text is given directly to "
                "the vision model as the pointing query — be specific about "
                "appearance and position, do NOT repeat the whole task."
            ),
        },
        "label": {
            "type": "integer",
            "description": (
                "SoM marker number to interact with. Only needed to disambiguate "
                "after an [ambiguous] response listed candidate labels."
            ),
        },
        "action": {
            "type": "string",
            "enum": ["click", "double_click", "right_click", "type", "scroll_down", "scroll_up"],
            "description": "What action to perform on the element.",
        },
        "text": {
            "type": "string",
            "description": "Text to type. Required when action is 'type'.",
        },
    },
    "required": ["action"],
}


UPGRADE_VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}


PREVIEW_POINTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "points": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {"type": "number"},
            },
            "description": (
                "1-3 candidate [x, y] coordinates in the CURRENT SCREENSHOT's "
                "coordinate space (the compressed resolution stated in the "
                "environment description, not physical pixels). Numbered markers "
                "are drawn on a clean copy of the screenshot and shown back to "
                "you so you can adjust before clicking."
            ),
        },
    },
    "required": ["points"],
}


COMPLETE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": (
                "The final answer to return to the user. Calling this tool ends "
                "the turn immediately and skips verification, so only use it for "
                "purely conversational turns or when no screen/file action was "
                "needed and nothing needs verifying."
            ),
        },
    },
    "required": ["answer"],
}


REQUEST_HUMAN_HELP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "The question shown to the human, e.g. "
                "'是否已经手动完成知乎登录？'."
            ),
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "2-4 mutually exclusive choices for the human. Do NOT include a "
                "free-text option: the CLI always appends 'type something' itself."
            ),
        },
    },
    "required": ["question", "options"],
}


CODERUNNER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "Python code to execute.",
        },
        "language": {
            "type": "string",
            "enum": ["python", "javascript"],
            "description": "Programming language. Only python is currently supported locally.",
        },
    },
    "required": ["code"],
}


ALLOWED_MODULES = {
    "math",
    "random",
    "datetime",
    "json",
    "re",
    "statistics",
    "fractions",
    "decimal",
    "itertools",
    "collections",
    "functools",
    "typing",
    "hashlib",
    "string",
}

DISALLOWED_NAMES = {
    "__import__",
    "open",
    "exec",
    "eval",
    "compile",
    "input",
    "raw_input",
    "exit",
    "quit",
    "breakpoint",
}

# Runtime builtins that may still be present in a subprocess but that we do not
# want user code to rely on. This list is advisory; AST validation catches direct
# calls, but sandboxing happens in the subprocess with PYTHONSAFEPATH.
_RESTRICTED_BUILTINS = {
    "__import__",
    "open",
    "exec",
    "eval",
    "compile",
    "help",
    "license",
    "copyright",
    "credits",
}


class UnsafeCodeError(Exception):
    """Raised when code fails static security checks."""


class CodeRunner:
    """Local sandboxed code execution tool.

    The Python sandbox (AST validation + restricted builtins + import
    whitelist in a subprocess) is a best-effort boundary, NOT a hard
    security guarantee: dunder attribute traversal (e.g. via ``__class__``)
    can potentially regain dangerous modules. Treat all model-generated code
    as untrusted; the subprocess timeout and cwd isolation are the real
    containment. JavaScript has no sandbox at all and is gated behind
    --yes/--yes-all (see ``allow_javascript``).
    """

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        max_code_length: int = 4000,
        allowed_modules: set[str] | None = None,
        disallowed_names: set[str] | None = None,
        cwd: str | None = None,
        allow_javascript: bool = False,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_code_length = max_code_length
        self.allowed_modules = allowed_modules or ALLOWED_MODULES
        self.disallowed_names = disallowed_names or DISALLOWED_NAMES
        self.cwd = cwd
        # JavaScript runs via `node -e` with NO sandbox (no AST validation, no
        # import restriction, full environment). It is therefore gated behind
        # the user's explicit --yes/--yes-all auto-approve opt-in.
        self.allow_javascript = allow_javascript

    def _ensure_cwd(self) -> None:
        """Create the configured working directory if it does not exist yet."""
        if self.cwd is not None:
            os.makedirs(self.cwd, exist_ok=True)

    def run(
        self,
        code: str,
        language: str = "python",
        env: dict[str, str] | None = None,
    ) -> str:
        """Run code in a sandbox and return stdout/stderr."""
        language = language.lower()
        if language == "javascript":
            if not self.allow_javascript:
                return (
                    "[error] JavaScript execution runs WITHOUT sandbox "
                    "restrictions and is only available when the agent was "
                    "started with --yes or --yes-all. Use Python instead, or "
                    "restart with --yes."
                )
            return self._run_javascript(code, env=env)
        if language != "python":
            return f"[error] Language {language} is not supported."

        if len(code) > self.max_code_length:
            return "[error] Code snippet too long."

        code = self._sanitize(code)
        try:
            self._validate_ast(code)
        except UnsafeCodeError as exc:
            return f"[error] {exc}"

        script = self._wrap_in_restricted_env(code)
        self._ensure_cwd()
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired:
            return "[error] Code execution timed out."
        except Exception as exc:
            return f"[error] {exc}"

        output = []
        if proc.stdout:
            output.append(proc.stdout)
        if proc.stderr:
            output.append(f"[stderr] {proc.stderr}")
        return "\n".join(output) or "[ok] No output."

    @staticmethod
    def _sanitize(code: str) -> str:
        lines = code.splitlines()
        cleaned = []
        for line in lines:
            if line.startswith("#!") or line.startswith("# -*-"):
                continue
            cleaned.append(line)
        return "\n".join(cleaned)

    def _validate_ast(self, code: str) -> None:
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise UnsafeCodeError(f"Syntax error: {exc}") from exc

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in self.allowed_modules:
                        raise UnsafeCodeError(f"Import not allowed: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module is None or node.module.split(".")[0] not in self.allowed_modules:
                    raise UnsafeCodeError(f"Import not allowed: {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in self.disallowed_names:
                    raise UnsafeCodeError(f"Call to {func.id} is not allowed")
                if isinstance(func, ast.Attribute) and func.attr in self.disallowed_names:
                    raise UnsafeCodeError(f"Call to .{func.attr} is not allowed")
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in self.disallowed_names:
                    raise UnsafeCodeError(f"Use of {node.id} is not allowed")

    def _wrap_in_restricted_env(self, code: str) -> str:
        """Wrap user code so it executes without a few dangerous builtins."""
        restricted_lines = [f"{name} = None" for name in _RESTRICTED_BUILTINS]
        return "\n".join(restricted_lines + ["", code])

    def _run_javascript(self, code: str, env: dict[str, str] | None = None) -> str:
        """Run JavaScript via node if available, otherwise return an error."""
        if len(code) > self.max_code_length:
            return "[error] Code snippet too long."
        node = shutil.which("node")
        if not node:
            return "[error] JavaScript execution requires Node.js, which was not found."
        self._ensure_cwd()
        try:
            proc = subprocess.run(
                [node, "-e", code],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired:
            return "[error] Code execution timed out."
        except Exception as exc:
            return f"[error] {exc}"

        output = []
        if proc.stdout:
            output.append(proc.stdout)
        if proc.stderr:
            output.append(f"[stderr] {proc.stderr}")
        return "\n".join(output) or "[ok] No output."


class RestrictedCodeRunner(CodeRunner):
    """CodeRunner with additional runtime import restrictions in the subprocess."""

    def run(self, code: str, language: str = "python") -> str:
        return super().run(
            code,
            language=language,
            env={**os.environ, "PYTHONSAFEPATH": "1"},
        )

    def _wrap_in_restricted_env(self, code: str) -> str:
        """Wrap user code so it runs in a fresh namespace with restricted imports and builtins."""
        import textwrap

        allowed_modules = sorted(self.allowed_modules)
        blocked_builtins = sorted(_RESTRICTED_BUILTINS)
        wrapper = textwrap.dedent(f'''
        import builtins, importlib, sys
        _ALLOWED_MODULES = frozenset({allowed_modules!r})
        _BLOCKED_BUILTINS = frozenset({blocked_builtins!r})
        _ORIGINAL_IMPORT = builtins.__import__

        # Pre-load whitelisted modules (and their stdlib dependencies) before
        # locking down imports, so user code can use them without needing sys.path.
        for _mod in sorted(_ALLOWED_MODULES):
            try:
                importlib.import_module(_mod)
            except Exception as exc:
                logger.debug("Failed to pre-load module %s: %s", _mod, exc)

        class _RestrictedFinder:
            def find_spec(self, name, path, target=None):
                root = name.split(".")[0]
                if root not in _ALLOWED_MODULES:
                    raise ModuleNotFoundError("Import not allowed: " + name)
                return None

        sys.meta_path.insert(0, _RestrictedFinder())
        sys.path = []

        _safe_builtins = {{k: v for k, v in builtins.__dict__.items() if k not in _BLOCKED_BUILTINS}}

        # Use a default argument to capture the original import function so the
        # wrapper function does not close over module globals.
        def _restricted_import(
            name, globals=None, locals=None, fromlist=(), level=0,
            _orig_import=_ORIGINAL_IMPORT, _allowed=_ALLOWED_MODULES,
        ):
            root = name.split(".")[0]
            if root not in _allowed:
                raise ImportError("Import not allowed: " + name)
            return _orig_import(name, globals, locals, fromlist, level)

        _safe_builtins["__import__"] = _restricted_import

        # Capture exec before we strip module builtins; otherwise the exec call
        # below would not be able to resolve the builtin name.
        _exec = exec

        # Remove dangerous names from the wrapper module globals so that
        # __import__.__globals__ cannot be used to escape the sandbox.
        for _name in (
            "__builtins__", "builtins", "importlib", "sys", "_RestrictedFinder",
            "_ORIGINAL_IMPORT", "_BLOCKED_BUILTINS",
        ):
            globals().pop(_name, None)

        # Execute user code in a fresh namespace so helper variables above do not leak.
        _exec(compile({code!r}, "<sandbox>", "exec"), {{"__builtins__": _safe_builtins}})
        ''')
        return wrapper


# Backwards-compatible helper; runs with the process cwd (registration uses
# register_all, which passes the configured cache directory as cwd).
def run_code(code: str, language: str = "python") -> str:
    return RestrictedCodeRunner().run(code, language=language)


# Windows-MCP positional tools whose `label` argument is only valid against
# the most recent Snapshot: any new Snapshot/Screenshot rebuilds the label
# space, so acting on a stale label fails with "Label N out of range".
_WINDOWS_LABEL_TOOLS = {"Click", "Type", "Scroll", "Move"}

_LABEL_FRESHNESS_NOTE = (
    " IMPORTANT: a `label` is only valid for the MOST RECENT windows__Snapshot "
    "— any new Snapshot/Screenshot invalidates all previous labels. Call this "
    "tool immediately after Snapshot, and re-Snapshot if anything changed the "
    "screen in between."
)


def build_mcp_tools(mcp: "MCPMultiplexer") -> list[dict[str, Any]]:
    """Convert MCP tool schemas into OpenAI function tool definitions."""
    tools = []
    for tool in mcp.all_tools():
        name = f"{tool['server']}__{tool['name']}"
        description = (
            tool.get("description", "")
            or f"Call {tool['name']} on {tool['server']} MCP server"
        )
        if tool["server"] == "windows" and tool["name"] in _WINDOWS_LABEL_TOOLS:
            description += _LABEL_FRESHNESS_NOTE
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": tool.get("schema", {"type": "object"}),
            },
        })
    return tools


def register_all(
    llm: Any,
    mcp: "MCPMultiplexer",
    code_cwd: str | None = None,
    allow_javascript: bool = False,
) -> None:
    """Register MCP tools and local CodeRunner with the LLM client.

    ``code_cwd`` is the working directory for CodeRunner subprocesses (the
    cache directory in production); relative paths written by generated code
    land there instead of the process cwd. ``allow_javascript`` should reflect
    the user's --yes/--yes-all opt-in: unsandboxed Node execution is only
    enabled then.
    """
    llm.register_function_tools(build_mcp_tools(mcp))
    runner = RestrictedCodeRunner(cwd=code_cwd, allow_javascript=allow_javascript)
    llm.register_local_function(
        "CodeRunner",
        runner.run,
        schema=CODERUNNER_SCHEMA,
        description=(
            "Run a short Python or JavaScript snippet in a local sandbox and "
            "return output. Python is fully sandboxed; JavaScript requires "
            "Node.js and is only available in --yes/--yes-all mode."
        ),
    )
