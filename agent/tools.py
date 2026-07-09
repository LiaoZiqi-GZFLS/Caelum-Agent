"""Tool definitions: MCP tool mapping and local CodeRunner.

CodeRunner executes Python snippets in a subprocess sandbox. The snippet is
first checked by an AST validator that limits imports and disallows dangerous
built-ins. Execution is bounded by a timeout and returns stdout/stderr.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp_client import MCPMultiplexer


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
_RESTRICTED_BUILTINS = [
    "__import__",
    "open",
    "exec",
    "eval",
    "compile",
]


class UnsafeCodeError(Exception):
    """Raised when code fails static security checks."""


class CodeRunner:
    """Local sandboxed code execution tool."""

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        max_code_length: int = 4000,
        allowed_modules: set[str] | None = None,
        disallowed_names: set[str] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_code_length = max_code_length
        self.allowed_modules = allowed_modules or ALLOWED_MODULES
        self.disallowed_names = disallowed_names or DISALLOWED_NAMES

    def run(self, code: str, language: str = "python") -> str:
        """Run code in a sandbox and return stdout/stderr."""
        language = language.lower()
        if language == "javascript":
            return self._run_javascript(code)
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
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
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

    def _run_javascript(self, code: str) -> str:
        """Run JavaScript via node if available, otherwise return an error."""
        if len(code) > self.max_code_length:
            return "[error] Code snippet too long."
        node = shutil.which("node")
        if not node:
            return "[error] JavaScript execution requires Node.js, which was not found."
        try:
            proc = subprocess.run(
                [node, "-e", code],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
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

    def _wrap_in_restricted_env(self, code: str) -> str:
        restricted_lines = [f"builtins.{name} = None" for name in _RESTRICTED_BUILTINS if name != "__import__"]
        allowed_modules_literal = ", ".join(repr(m) for m in sorted(self.allowed_modules))
        wrapper = """
import builtins, sys
_ALLOWED_MODULES = {{{allowed_modules}}}
_ORIGINAL_IMPORT = builtins.__import__
def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in _ALLOWED_MODULES:
        raise ImportError("Import not allowed: " + name)
    return _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)
builtins.__import__ = _restricted_import
for _mod in sorted(_ALLOWED_MODULES):
    try:
        __import__(_mod)
    except Exception:
        pass
sys.path = []
for _name in list(sys.modules):
    if _name not in _ALLOWED_MODULES and _name not in ("builtins", "sys", "__main__"):
        del sys.modules[_name]
{restricted_builtins}
{user_code}
""".format(
            allowed_modules=allowed_modules_literal,
            restricted_builtins="\n".join(restricted_lines),
            user_code=code,
        )
        return wrapper


# Backwards-compatible function used during registration.
def run_code(code: str, language: str = "python") -> str:
    return RestrictedCodeRunner().run(code, language=language)


def build_mcp_tools(mcp: "MCPMultiplexer") -> list[dict[str, Any]]:
    """Convert MCP tool schemas into OpenAI function tool definitions."""
    tools = []
    for tool in mcp.all_tools():
        name = f"{tool['server']}__{tool['name']}"
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", "") or f"Call {tool['name']} on {tool['server']} MCP server",
                "parameters": tool.get("schema", {"type": "object"}),
            },
        })
    return tools


def register_all(llm: Any, mcp: "MCPMultiplexer") -> None:
    """Register MCP tools and local CodeRunner with the LLM client."""
    llm.register_function_tools(build_mcp_tools(mcp))
    llm.register_local_function(
        "CodeRunner",
        run_code,
        schema=CODERUNNER_SCHEMA,
        description="Run a short Python or JavaScript snippet in a local sandbox and return output. Python is fully sandboxed; JavaScript requires Node.js.",
    )
