#!/usr/bin/env python3
"""First-run setup for Caelum-Agent.

Run:
    python setup.py

It will:
1. Verify Python 3.12 (windows-mcp 0.8.2 requires >=3.12; the venv stays on
   3.12 for a fully supported combination).
2. Create a virtual environment under .venv/ (prefer uv, fall back to stdlib venv).
3. Install Python dependencies from requirements.txt.
   - windows-mcp 0.8.2 declares requires-python >=3.13, but it runs fine on 3.12.
     The installer therefore uses --ignore-requires-python for that package.
   - Afterwards, an idempotent patch fixes windows-mcp's upstream tree_node
     UnboundLocalError in the installed tree/service.py (skipped automatically
     if the layout is already fixed or unrecognized).
4. Copy config.yaml.example -> config.yaml if missing and prompt for your Kimi API key.
5. Validate config.yaml (parseable and contains a real API key).
6. Create data/ directory and a minimal SQLite schema.
7. Optionally download the vision weights (YOLO icon_detect ~40MB, Florence-2
   icon_caption ~1GB, Florence-2 processor ~3MB) — the GitHub Release mirror
   is tried first, HuggingFace is the fallback. Afterwards the Florence-2
   config's auto_map is localized so model loading works fully offline.
8. Install Playwright Chromium if not already present.
9. Run a smoke test (Kimi API + Windows-MCP tool list + YOLO load).

For non-interactive installs you can pass the key on the command line:
    python setup.py --api-key sk-...

To skip smoke tests:
    python setup.py --skip-smoke-tests

For Playwright Chromium download behind the Chinese firewall:
    $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
    python setup.py

To download the vision weights (GitHub Release mirror first, HF fallback):
    python setup.py --download-weights
"""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from agent.config import Config, MCPServerConfig

PROJECT_ROOT = Path(__file__).resolve().parent
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
CONFIG_EXAMPLE = PROJECT_ROOT / "config.yaml.example"
CONFIG_FILE = PROJECT_ROOT / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
VENV_DIR = PROJECT_ROOT / ".venv"
PYTHON_EXE = VENV_DIR / "Scripts" / "python.exe" if platform.system() == "Windows" else VENV_DIR / "bin" / "python"
PIP_EXE = VENV_DIR / "Scripts" / "pip.exe" if platform.system() == "Windows" else VENV_DIR / "bin" / "pip"

REQUIRED_PYTHON = (3, 12)


def log(message: str) -> None:
    print(f"[setup] {message}")


def run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command, returning its result."""
    log(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, text=True, cwd=cwd or PROJECT_ROOT, env=env)


def check_python_version() -> None:
    version = sys.version_info[:2]
    if version != REQUIRED_PYTHON:
        raise RuntimeError(
            f"Caelum-Agent requires Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}. "
            f"You are running {version[0]}.{version[1]}. "
            "Please switch to Python 3.12 and rerun setup.py."
        )
    log(f"Python version OK: {sys.version.split()[0]}")


def find_uv() -> str | None:
    uv = shutil.which("uv")
    if uv:
        return uv
    # Also check common install locations on Windows
    candidates = [
        Path.home() / ".cargo" / "bin" / "uv.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "uv" / "uv.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def create_venv(uv_path: str | None) -> None:
    if VENV_DIR.exists():
        log("Virtual environment already exists; skipping creation.")
        return

    log("Creating virtual environment with Python 3.12...")
    if uv_path:
        run([uv_path, "venv", "--python", "3.12", str(VENV_DIR)])
    else:
        run([sys.executable, "-m", "venv", str(VENV_DIR)])

    if not PYTHON_EXE.exists():
        raise RuntimeError(f"Virtual environment python not found at {PYTHON_EXE}")

    # Ensure pip is available inside the venv (uv venv sometimes omits pip)
    try:
        run([str(PYTHON_EXE), "-m", "pip", "--version"])
    except Exception:
        log("pip not found in venv; bootstrapping pip...")
        run([str(PYTHON_EXE), "-m", "ensurepip", "--upgrade"])


def install_python_deps(uv_path: str | None) -> None:
    if not REQUIREMENTS.exists():
        raise RuntimeError(f"{REQUIREMENTS} not found.")

    log("Installing Python dependencies...")

    if uv_path:
        # uv cannot install windows-mcp on 3.12 without ignoring requires-python.
        # We therefore install everything else with uv, then windows-mcp via pip.
        windows_mcp_line = None
        other_lines = []
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("windows-mcp"):
                windows_mcp_line = stripped
            elif stripped and not stripped.startswith("#"):
                other_lines.append(stripped)

        temp_req = PROJECT_ROOT / ".requirements-temp.txt"
        temp_req.write_text("\n".join(other_lines) + "\n", encoding="utf-8")
        try:
            run([uv_path, "pip", "install", "--python", str(VENV_DIR), "-r", str(temp_req)])
        finally:
            temp_req.unlink(missing_ok=True)

        if windows_mcp_line:
            run([
                str(PIP_EXE), "install", "--ignore-requires-python", windows_mcp_line
            ])
    else:
        # Plain pip path: install all, ignoring requires-python for the whole req file.
        # This is safe because every other package supports 3.12; only windows-mcp needs the waiver.
        run([str(PIP_EXE), "install", "--ignore-requires-python", "-r", str(REQUIREMENTS)])


def copy_config() -> None:
    if CONFIG_FILE.exists():
        log(f"{CONFIG_FILE.name} already exists; skipping copy.")
        return
    if not CONFIG_EXAMPLE.exists():
        raise RuntimeError(f"{CONFIG_EXAMPLE.name} not found.")
    shutil.copy(CONFIG_EXAMPLE, CONFIG_FILE)
    log(f"Copied {CONFIG_EXAMPLE.name} -> {CONFIG_FILE.name}.")


def prompt_for_api_key(api_key: str | None = None) -> str | None:
    """Return a non-empty API key, prompting the user if necessary."""
    if api_key:
        return api_key.strip()
    if not sys.stdin.isatty():
        return None
    print("\nPlease enter your Kimi API key (starts with sk-). Leave blank to configure manually later.")
    try:
        key = input("Kimi API key: ").strip()
    except EOFError:
        return None
    return key or None


def inject_api_key_into_config(api_key: str) -> None:
    """Replace the placeholder API key in config.yaml with the provided key."""
    if not CONFIG_FILE.exists():
        raise RuntimeError(f"{CONFIG_FILE.name} not found; cannot inject API key.")
    text = CONFIG_FILE.read_text(encoding="utf-8")
    marker = "api_key: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    if marker not in text:
        # If the placeholder is already gone, leave the file untouched.
        return
    updated = text.replace(marker, f"api_key: {api_key}")
    CONFIG_FILE.write_text(updated, encoding="utf-8")
    log("API key written to config.yaml.")


def validate_config() -> tuple[bool, str]:
    """Load config.yaml and verify a real API key is present."""
    if not CONFIG_FILE.exists():
        return False, f"{CONFIG_FILE.name} not found."
    try:
        config = Config.from_yaml(CONFIG_FILE)
    except Exception as exc:
        return False, f"Failed to parse {CONFIG_FILE.name}: {exc}"

    api_key = config.llm.api_key.strip()
    if not api_key or api_key == "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx":
        return False, "Kimi API key is missing or still set to the placeholder value."
    return True, "config.yaml is valid."


def create_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db_path = DATA_DIR / "memory.db"
    if not db_path.exists():
        log("Initializing SQLite database...")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_summary TEXT NOT NULL,
                failure_reason TEXT,
                fix_action TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                result TEXT
            );
            CREATE TABLE IF NOT EXISTS state_persistence (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# windows-mcp tree_node patch
# ---------------------------------------------------------------------------
# windows-mcp 0.8.2 has an upstream bug in tree/service.py: `tree_node` is only
# assigned inside `if name:`, but the semantic-node block that dereferences it
# is a *sibling* of that branch, so any nameless interactive element with a
# semantic parent raises `UnboundLocalError: cannot access local variable
# 'tree_node'` on every Snapshot (the window's subtree is dropped and the
# server's stderr fills with tracebacks). The fix nests the semantic block
# under `if name:`. We patch the installed copy idempotently after dependency
# installation; see docs/windows_mcp/upstream-tree-node-issue.md.

_TREE_APPEND_LINE = "interactive_nodes.append(tree_node)"
_TREE_SEMANTIC_IF = "if current_semantic_node is not None:"


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def analyze_tree_service(source: str) -> str:
    """Classify windows-mcp tree/service.py source: "buggy" | "fixed" | "unknown"."""
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != _TREE_APPEND_LINE:
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines) or not lines[j].strip().startswith(_TREE_SEMANTIC_IF):
            continue
        if _indent_width(lines[j]) < _indent_width(line):
            return "buggy"  # sibling `if`: tree_node may be unbound
        return "fixed"  # nested under `if name:` (or deeper)
    return "unknown"


def fix_tree_service(source: str) -> str:
    """Nest the semantic-node block under `if name:`. Raises ValueError if the
    source is not in the known buggy layout (never guess at third-party code)."""
    if analyze_tree_service(source) != "buggy":
        raise ValueError("source is not in the known buggy tree_node layout")
    lines = source.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() != _TREE_APPEND_LINE:
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if not lines[j].strip().startswith(_TREE_SEMANTIC_IF):
            continue
        if _indent_width(lines[j]) >= _indent_width(line):
            continue
        delta = _indent_width(line) - _indent_width(lines[j])
        base = _indent_width(lines[j])
        # Re-indent the semantic `if` line and its body (everything more
        # indented than it, blank lines included untouched) one level deeper.
        lines[j] = " " * delta + lines[j]
        for k in range(j + 1, len(lines)):
            body = lines[k]
            if body.strip() and _indent_width(body) <= base:
                break
            if body.strip():
                lines[k] = " " * delta + body
        break
    return "".join(lines)


def patch_windows_mcp_tree(service_path: Path) -> str:
    """Patch one tree/service.py file. Returns a status string:
    "patched" | "already_fixed" | "unknown_layout"."""
    source = service_path.read_text(encoding="utf-8")
    status = analyze_tree_service(source)
    if status == "fixed":
        return "already_fixed"
    if status == "unknown":
        return "unknown_layout"
    service_path.write_text(fix_tree_service(source), encoding="utf-8")
    return "patched"


def locate_windows_mcp_tree() -> Path | None:
    """Find the installed windows_mcp/tree/service.py (venv first, then the
    current interpreter's site-packages). None when windows-mcp is absent."""
    candidates: list[Path] = []
    if platform.system() == "Windows":
        candidates.append(
            VENV_DIR / "Lib" / "site-packages" / "windows_mcp" / "tree" / "service.py"
        )
    else:
        candidates.append(
            VENV_DIR
            / "lib"
            / f"python{REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}"
            / "site-packages"
            / "windows_mcp"
            / "tree"
            / "service.py"
        )
    try:
        import importlib.util

        spec = importlib.util.find_spec("windows_mcp")
        if spec is not None and spec.origin:
            candidates.append(Path(spec.origin).parent / "tree" / "service.py")
    except ImportError:
        pass
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def patch_windows_mcp(locate=None) -> str:
    """Patch the installed windows-mcp. Returns a status string:
    "patched" | "already_fixed" | "unknown_layout" | "not_installed"."""
    finder = locate or locate_windows_mcp_tree
    service_path = finder()
    if service_path is None:
        return "not_installed"
    return patch_windows_mcp_tree(service_path)


def _probe_dml_provider() -> bool:
    """Return True if the venv's onnxruntime exposes the DirectML provider."""
    out = subprocess.run(
        [
            str(PYTHON_EXE),
            "-c",
            "import onnxruntime as ort; "
            "print('DmlExecutionProvider' in ort.get_available_providers())",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return out.returncode == 0 and out.stdout.strip().endswith("True")


def ensure_onnxruntime_dml(probe=None, run_cmd=None, is_windows=None) -> str:
    """Swap CPU onnxruntime for onnxruntime-directml so OCR can run on GPU.

    rapidocr-onnxruntime declares plain ``onnxruntime`` as a dependency, so a
    fresh install always lands the CPU build; the DirectML build must replace
    it afterwards (the two distributions share the ``onnxruntime`` package
    directory). Returns "installed" | "already_ok" | "skipped" | "failed".
    Best-effort: any failure leaves CPU OCR working — rapidocr falls back to
    the CPU provider at runtime when DML is unavailable.
    """
    if is_windows is None:
        is_windows = sys.platform == "win32"
    if not is_windows:
        return "skipped"
    run_cmd = run_cmd or run
    try:
        has_dml = probe() if probe is not None else _probe_dml_provider()
        if has_dml:
            return "already_ok"
        run_cmd([str(PIP_EXE), "uninstall", "-y", "onnxruntime"])
        run_cmd([str(PIP_EXE), "install", "onnxruntime-directml"])
        return "installed"
    except Exception:
        return "failed"


def install_playwright_browser() -> None:
    log("Checking Playwright Chromium...")
    try:
        run([str(PYTHON_EXE), "-c", "from playwright.sync_api import sync_playwright; sync_playwright().start()"])
        log("Playwright already has a browser installed.")
        return
    except Exception:
        pass

    log("Installing Playwright Chromium (this may take a few minutes)...")
    env = os.environ.copy()
    # Respect user's existing mirror setting
    run(["npx", "playwright", "install", "chromium"], env=env, check=False)


# ---------------------------------------------------------------------------
# YOLO (OmniParser icon_detect) weight download
# ---------------------------------------------------------------------------

YOLO_WEIGHTS_URL = (
    "https://github.com/LiaoZiqi-GZFLS/omniparser-weights/"
    "releases/download/v2.0/icon_detect.zip"
)
YOLO_WEIGHTS_DIR = PROJECT_ROOT / "models" / "omniparser" / "icon_detect"
# The real model.pt is ~40MB; anything smaller is a truncated/placeholder file.
YOLO_MIN_MODEL_BYTES = 1_000_000


def yolo_weights_present(dir_path: Path) -> bool:
    """True when dir_path holds a plausible model.pt (exists and big enough)."""
    model = dir_path / "model.pt"
    return model.exists() and model.stat().st_size >= YOLO_MIN_MODEL_BYTES


def _fetch_url(url: str, dest: Path) -> None:
    """Download url to dest (stdlib only; follows redirects)."""
    with urllib.request.urlopen(url) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)


def _install_from_zip(zip_path: Path, marker: str, target_dir: Path) -> None:
    """Extract zip_path and copy the directory containing ``marker`` into
    target_dir (handles both nested and root-level zip layouts).
    Raises RuntimeError when the marker file is missing."""
    with tempfile.TemporaryDirectory() as tmp:
        extracted = Path(tmp) / "extracted"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extracted)
        candidates = sorted(extracted.rglob(marker))
        if not candidates:
            raise RuntimeError(f"{marker} not found inside {zip_path.name}")
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(candidates[0].parent, target_dir, dirs_exist_ok=True)


def download_yolo_weights(
    url: str = YOLO_WEIGHTS_URL,
    target_dir: Path = YOLO_WEIGHTS_DIR,
    fetch=None,
) -> bool:
    """Download and extract the OmniParser icon_detect YOLO weights.

    Idempotent: skips the download when a plausible model.pt already exists.
    Best-effort: any failure (network, bad zip, missing model.pt inside) logs
    a warning and returns False so setup can continue — the agent still works
    via UIA; only vision SoM annotation needs these weights.
    """
    if yolo_weights_present(target_dir):
        log("YOLO weights already present; skipping download.")
        return True
    fetch = fetch or _fetch_url
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "icon_detect.zip"
            log(f"Downloading YOLO weights from {url}...")
            fetch(url, zip_path)
            _install_from_zip(zip_path, "model.pt", target_dir)
        if not yolo_weights_present(target_dir):
            raise RuntimeError("model.pt missing or too small after extraction")
        log(f"YOLO weights installed to {target_dir}.")
        return True
    except Exception as exc:
        log(f"WARNING: YOLO weight download failed: {exc}")
        log(
            "The agent still works via UIA; vision SoM annotation needs "
            "models/omniparser/icon_detect/model.pt."
        )
        return False


# ---------------------------------------------------------------------------
# Florence-2 (OmniParser icon_caption) weight + processor download
# ---------------------------------------------------------------------------

ICON_CAPTION_REPO = "microsoft/OmniParser-v2.0"
ICON_CAPTION_SUBDIR = "icon_caption"
ICON_CAPTION_DIR = PROJECT_ROOT / "models" / "omniparser" / "icon_caption"
ICON_CAPTION_WEIGHTS_URL = (
    "https://github.com/LiaoZiqi-GZFLS/omniparser-weights/"
    "releases/download/v2.0/icon_caption_florence.zip"
)
# The icon_caption checkpoint ships no processor files; the processor and the
# trust_remote_code modeling code both live in this HF repo. The release zip
# mirrors its small files (preferred — GitHub stays reachable where
# huggingface.co is blocked); the HF cache warm-up is the fallback.
ICON_CAPTION_PROCESSOR_REPO = "microsoft/Florence-2-base-ft"
ICON_CAPTION_PROCESSOR_PATTERNS = ["*.json", "*.py", "*.txt", "*.model"]
ICON_CAPTION_PROCESSOR_URL = (
    "https://github.com/LiaoZiqi-GZFLS/omniparser-weights/"
    "releases/download/v2.0/icon_caption_processor.zip"
)
ICON_CAPTION_PROCESSOR_DIR = PROJECT_ROOT / "models" / "omniparser" / "icon_caption_processor"


def icon_caption_weights_present(dir_path: Path) -> bool:
    """True when dir_path holds a plausible Florence-2 checkpoint."""
    return (dir_path / "config.json").exists() and any(dir_path.glob("*.safetensors"))


def download_icon_caption_weights(
    url: str = ICON_CAPTION_WEIGHTS_URL,
    repo: str = ICON_CAPTION_REPO,
    target_dir: Path = ICON_CAPTION_DIR,
    fetch=None,
    snapshot_download=None,
) -> bool:
    """Download the OmniParser icon_caption Florence-2 fine-tune (~1GB).

    Release-first: the GitHub Release zip mirror is tried before HuggingFace
    (GitHub stays reachable where huggingface.co is blocked). Idempotent:
    skips when a plausible checkpoint already exists. Best-effort: returns
    False when both sources fail — icon captioning is a nicety on top of YOLO
    markers, not a hard requirement. The HF path honors HF_ENDPOINT.
    """
    if icon_caption_weights_present(target_dir):
        log("Florence-2 icon_caption weights already present; skipping download.")
        return True
    fetch = fetch or _fetch_url
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "icon_caption_florence.zip"
            log(f"Downloading Florence-2 icon_caption weights from {url}...")
            fetch(url, zip_path)
            _install_from_zip(zip_path, "model.safetensors", target_dir)
        if not icon_caption_weights_present(target_dir):
            raise RuntimeError("icon_caption weights incomplete after extraction")
        log(f"Florence-2 icon_caption weights installed to {target_dir}.")
        return True
    except Exception as exc:
        log(f"WARNING: release download failed ({exc}); trying HuggingFace {repo}...")
    try:
        if snapshot_download is None:
            from huggingface_hub import snapshot_download as _snapshot_download

            snapshot_download = _snapshot_download
        with tempfile.TemporaryDirectory() as tmp:
            local = snapshot_download(
                repo,
                allow_patterns=f"{ICON_CAPTION_SUBDIR}/*",
                local_dir=tmp,
            )
            src = Path(local) / ICON_CAPTION_SUBDIR
            if not src.exists():
                raise RuntimeError(
                    f"{ICON_CAPTION_SUBDIR}/ not found in the downloaded snapshot"
                )
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target_dir, dirs_exist_ok=True)
        if not icon_caption_weights_present(target_dir):
            raise RuntimeError("icon_caption weights incomplete after download")
        log(f"Florence-2 icon_caption weights installed to {target_dir}.")
        return True
    except Exception as exc:
        log(f"WARNING: icon_caption weight download failed: {exc}")
        log(
            "Icon captioning needs models/omniparser/icon_caption; without it "
            "YOLO markers still work, they just lack semantic captions."
        )
        return False


def icon_caption_processor_present(dir_path: Path) -> bool:
    """True when dir_path holds the Florence-2 processor essentials."""
    return (dir_path / "processing_florence2.py").exists() and (
        dir_path / "tokenizer.json"
    ).exists()


def download_icon_caption_processor(
    url: str = ICON_CAPTION_PROCESSOR_URL,
    target_dir: Path = ICON_CAPTION_PROCESSOR_DIR,
    fetch=None,
    snapshot_download=None,
) -> bool:
    """Download the Florence-2 processor + remote modeling code (~3MB).

    Release zip first (installed into target_dir so the captioner can run
    fully offline); falls back to warming the HF cache — the captioner
    resolves a missing local dir to the HF repo id at runtime. Best-effort:
    returns False when both sources fail (the first caption then needs
    network, but everything else works).
    """
    if icon_caption_processor_present(target_dir):
        log("Florence-2 processor already present; skipping download.")
        return True
    fetch = fetch or _fetch_url
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "icon_caption_processor.zip"
            log(f"Downloading Florence-2 processor from {url}...")
            fetch(url, zip_path)
            _install_from_zip(zip_path, "processing_florence2.py", target_dir)
        if not icon_caption_processor_present(target_dir):
            raise RuntimeError("processor files incomplete after extraction")
        log(f"Florence-2 processor installed to {target_dir}.")
        return True
    except Exception as exc:
        log(
            f"WARNING: processor release download failed ({exc}); "
            "warming the HuggingFace cache instead..."
        )
    try:
        if snapshot_download is None:
            from huggingface_hub import snapshot_download as _snapshot_download

            snapshot_download = _snapshot_download
        snapshot_download(
            ICON_CAPTION_PROCESSOR_REPO,
            allow_patterns=ICON_CAPTION_PROCESSOR_PATTERNS,
        )
        log(f"Processor files cached from {ICON_CAPTION_PROCESSOR_REPO}.")
        return True
    except Exception as exc:
        log(f"WARNING: processor download failed (will fetch on first use): {exc}")
        return False


def localize_florence2_remote_code(
    icon_caption_dir: Path = ICON_CAPTION_DIR,
    processor_dir: Path = ICON_CAPTION_PROCESSOR_DIR,
) -> str:
    """Rewrite icon_caption/config.json auto_map to local remote-code refs.

    The checkpoint's auto_map points at microsoft/Florence-2-base-ft on HF
    (``<repo>--<module>.<Class>``), so even with local weights the model load
    fetches its modeling code from HF. Rewriting the refs to plain
    ``<module>.<Class>`` (no repo prefix — transformers then resolves the
    module relative to the model dir) and copying the modules from
    processor_dir makes the load fully offline. Values already carrying a
    bare ``--module`` prefix (an earlier broken rewrite) are repaired too.
    Idempotent. Returns "localized" | "already_local" | "no_config" |
    "missing_processor_files".
    """
    config_path = icon_caption_dir / "config.json"
    if not config_path.exists():
        return "no_config"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    auto_map = config.get("auto_map") or {}
    remote = {
        key: value
        for key, value in auto_map.items()
        if isinstance(value, str) and "--" in value
    }
    if not remote:
        return "already_local"
    modules = {value.split("--", 1)[1].rsplit(".", 1)[0] for value in remote.values()}
    if any(not (processor_dir / f"{module}.py").exists() for module in modules):
        return "missing_processor_files"
    for module in modules:
        shutil.copy(processor_dir / f"{module}.py", icon_caption_dir / f"{module}.py")
    for key, value in remote.items():
        auto_map[key] = value.split("--", 1)[1]
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return "localized"


def smoke_test_kimi() -> bool:
    log("Running Kimi API smoke test...")
    script = PROJECT_ROOT / "spikes" / "kimi_formula_chain.py"
    if not script.exists():
        log("Skipping Kimi smoke test: spikes/kimi_formula_chain.py not found.")
        return True
    try:
        result = run([str(PYTHON_EXE), str(script)], check=False)
        return result.returncode == 0
    except Exception as exc:
        log(f"Kimi smoke test failed: {exc}")
        return False


def smoke_test_mcp_servers() -> bool:
    log("Running MCP server smoke tests...")
    from agent.config import Config

    try:
        config = Config.from_yaml(CONFIG_FILE)
    except Exception as exc:
        log(f"Failed to load config.yaml for smoke test: {exc}")
        return False

    all_ok = True
    from mcp_client import MCPClient

    for name, server_config in config.mcp_servers.model_dump().items():
        cfg = MCPServerConfig(**server_config)
        client = MCPClient(name, cfg, max_retries=2, base_delay=0.5)
        try:
            ok = asyncio.run(client.connect())
            if ok:
                log(f"  {name}: OK ({len(client.tools())} tools)")
            else:
                log(f"  {name}: FAILED to connect")
                all_ok = False
        except Exception as exc:
            log(f"  {name}: ERROR {exc}")
            all_ok = False
        finally:
            asyncio.run(client.disconnect())
    return all_ok


def smoke_test_yolo() -> bool:
    log("Running YOLO (OmniParser icon_detect) smoke test...")
    model = YOLO_WEIGHTS_DIR / "model.pt"
    if not model.exists():
        log("  YOLO weights not found; skipping.")
        return True
    code = (
        "from ultralytics import YOLO; "
        "from PIL import Image; "
        f"m = YOLO(r'{model}'); "
        "r = m.predict(Image.new('RGB', (64, 64)), imgsz=64, verbose=False); "
        "print('boxes:', len(r[0].boxes))"
    )
    try:
        result = run([str(PYTHON_EXE), "-c", code], check=False)
        return result.returncode == 0
    except Exception as exc:
        log(f"YOLO smoke test failed: {exc}")
        return False


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Caelum-Agent first-run setup")
    parser.add_argument(
        "--download-weights",
        action="store_true",
        help="Download OmniParser vision weights (YOLO icon_detect ~40MB + Florence-2 icon_caption ~1GB + processor ~3MB; GitHub Release mirror first, HuggingFace fallback) after environment setup.",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Kimi API key to write into config.yaml (skips interactive prompt).",
    )
    parser.add_argument(
        "--skip-smoke-tests",
        action="store_true",
        help="Skip smoke tests after setup.",
    )
    parser.add_argument(
        "--skip-prompts",
        action="store_true",
        help="Skip interactive prompts (useful for CI).",
    )
    return parser


def main() -> int:
    args = build_argparser().parse_args()

    log("Starting Caelum-Agent setup...")
    log(f"Project root: {PROJECT_ROOT}")

    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    uv_path = find_uv()
    if uv_path:
        log(f"Found uv at {uv_path}")
    else:
        log("uv not found; falling back to stdlib venv + pip.")
        log("For faster installs, install uv: https://docs.astral.sh/uv/getting-started/installation/")

    try:
        create_venv(uv_path)
        install_python_deps(uv_path)

        patch_status = patch_windows_mcp()
        if patch_status == "patched":
            log("Patched windows-mcp tree/service.py (upstream tree_node "
                "UnboundLocalError; see docs/windows_mcp/upstream-tree-node-issue.md).")
        elif patch_status == "already_fixed":
            log("windows-mcp tree_node patch: already applied (or fixed upstream).")
        elif patch_status == "unknown_layout":
            log("WARNING: windows-mcp tree/service.py has an unexpected layout; "
                "skipped the tree_node patch (upstream may have changed).")

        dml_status = ensure_onnxruntime_dml()
        if dml_status == "installed":
            log("Installed onnxruntime-directml (GPU OCR via DirectML, "
                "replacing CPU onnxruntime).")
        elif dml_status == "failed":
            log("WARNING: onnxruntime-directml install failed; OCR will run "
                "on CPU. You can retry: pip uninstall onnxruntime && "
                "pip install onnxruntime-directml")

        copy_config()

        # Configure API key: command-line arg > interactive prompt > leave placeholder.
        api_key = args.api_key
        if not api_key and not args.skip_prompts:
            api_key = prompt_for_api_key()
        if api_key:
            inject_api_key_into_config(api_key)

        config_ok, config_message = validate_config()
        if not config_ok:
            log(f"WARNING: {config_message}")
            if not args.skip_prompts:
                log("You can rerun setup with --api-key or edit config.yaml manually.")
        else:
            log(config_message)

        create_data_dir()
        if args.download_weights:
            download_yolo_weights()
            download_icon_caption_weights()
            download_icon_caption_processor()
            localize_status = localize_florence2_remote_code()
            if localize_status == "localized":
                log("Localized Florence-2 remote code: icon_caption/config.json "
                    "auto_map now points at local modules (fully offline).")
            elif localize_status == "missing_processor_files":
                log("WARNING: processor files missing; skipped Florence-2 auto_map "
                    "localization (model load will fetch remote code from HF).")
        install_playwright_browser()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: Command failed: {exc.cmd}\n{exc.stdout or ''}\n{exc.stderr or ''}", file=sys.stderr)
        return 1

    if args.skip_smoke_tests:
        log("Skipping smoke tests (--skip-smoke-tests).")
        log("=" * 50)
        log("Setup complete.")
        log("Activate the environment with:")
        if platform.system() == "Windows":
            log("    .venv\\Scripts\\activate")
        else:
            log("    source .venv/bin/activate")
        log("Then run the agent with: python main.py")
        log("=" * 50)
        return 0

    kimi_ok = smoke_test_kimi()
    mcp_ok = smoke_test_mcp_servers()
    yolo_ok = smoke_test_yolo()

    log("=" * 50)
    log("Setup complete.")
    if not kimi_ok:
        log("WARNING: Kimi API smoke test did not finish successfully. Check config.yaml and your API key.")
    if not mcp_ok:
        log("WARNING: One or more MCP server smoke tests failed.")
    if not yolo_ok:
        log("WARNING: YOLO smoke test did not finish successfully.")
    if kimi_ok and mcp_ok and yolo_ok:
        log("All smoke tests passed.")
    log("Activate the environment with:")
    if platform.system() == "Windows":
        log("    .venv\\Scripts\\activate")
    else:
        log("    source .venv/bin/activate")
    log("Then run the agent with: python main.py")
    log("=" * 50)

    return 0 if (kimi_ok and mcp_ok and yolo_ok) else 2


if __name__ == "__main__":
    sys.exit(main())
