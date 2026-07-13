"""Tests for setup.py helpers."""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import setup


@pytest.fixture
def temp_config(tmp_path: Path, monkeypatch: Any) -> tuple[Path, Path]:
    example = tmp_path / "config.yaml.example"
    config = tmp_path / "config.yaml"
    example.write_text(
        "llm:\n"
        "  provider: kimi\n"
        "  base_url: https://api.moonshot.cn/v1\n"
        "  api_key: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        "  model: kimi-k2.6\n"
        "mcp_servers:\n"
        "  playwright:\n"
        "    command: npx\n"
        "    args: []\n"
        "    env: {}\n"
        "  windows:\n"
        "    command: windows-mcp\n"
        "    args: [serve]\n"
        "    env: {}\n"
        "  filesystem:\n"
        "    command: npx\n"
        "    args: []\n"
        "    env: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup, "CONFIG_EXAMPLE", example)
    monkeypatch.setattr(setup, "CONFIG_FILE", config)
    return example, config


def test_copy_config_creates_file_from_example(temp_config: tuple[Path, Path]) -> None:
    example, config = temp_config
    assert not config.exists()

    setup.copy_config()

    assert config.exists()
    assert "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" in config.read_text(encoding="utf-8")


def test_copy_config_skips_existing_file(temp_config: tuple[Path, Path]) -> None:
    example, config = temp_config
    config.write_text("existing: true", encoding="utf-8")

    setup.copy_config()

    assert config.read_text(encoding="utf-8") == "existing: true"


def test_inject_api_key_replaces_placeholder(temp_config: tuple[Path, Path]) -> None:
    _, config = temp_config
    setup.copy_config()

    setup.inject_api_key_into_config("sk-real-key")

    text = config.read_text(encoding="utf-8")
    assert "api_key: sk-real-key" in text
    assert "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in text


def test_validate_config_fails_with_placeholder(temp_config: tuple[Path, Path]) -> None:
    _, config = temp_config
    setup.copy_config()

    ok, message = setup.validate_config()

    assert not ok
    assert "placeholder" in message


def test_validate_config_passes_with_real_key(temp_config: tuple[Path, Path]) -> None:
    _, config = temp_config
    setup.copy_config()
    setup.inject_api_key_into_config("sk-real-key")

    ok, message = setup.validate_config()

    assert ok
    assert "valid" in message


def test_validate_config_fails_when_file_missing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(setup, "CONFIG_FILE", tmp_path / "missing.yaml")

    ok, message = setup.validate_config()

    assert not ok
    assert "not found" in message


def test_prompt_for_api_key_returns_arg() -> None:
    assert setup.prompt_for_api_key("  sk-from-arg  ") == "sk-from-arg"


def test_prompt_for_api_key_reads_input(monkeypatch: Any) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "sk-from-stdin")

    assert setup.prompt_for_api_key() == "sk-from-stdin"


def test_prompt_for_api_key_returns_none_on_eof(monkeypatch: Any) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))

    assert setup.prompt_for_api_key() is None


def test_prompt_for_api_key_returns_none_when_not_tty(monkeypatch: Any) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert setup.prompt_for_api_key() is None


# ---------------------------------------------------------------------------
# windows-mcp tree_node patch (upstream UnboundLocalError fix)
# ---------------------------------------------------------------------------

# Mirrors the buggy layout in windows-mcp 0.8.2 tree/service.py: the semantic
# block sits as a *sibling* of `if name:` although it dereferences tree_node.
BUGGY_TREE_SERVICE = """\
                                if name:
                                    tree_node=TreeElementNode(**{
                                        'name':name,
                                    })
                                    interactive_nodes.append(tree_node)
                                if current_semantic_node is not None:
                                    current_semantic_node.add_child(SemanticNode(
                                        control_type=tree_node.control_type,
                                        metadata=dict(tree_node.metadata),
                                    ))
                                    semantic_added = True

                    # Informative Check
"""

FIXED_TREE_SERVICE = """\
                                if name:
                                    tree_node=TreeElementNode(**{
                                        'name':name,
                                    })
                                    interactive_nodes.append(tree_node)
                                    if current_semantic_node is not None:
                                        current_semantic_node.add_child(SemanticNode(
                                            control_type=tree_node.control_type,
                                            metadata=dict(tree_node.metadata),
                                        ))
                                        semantic_added = True

                    # Informative Check
"""


def test_analyze_tree_service_detects_buggy() -> None:
    assert setup.analyze_tree_service(BUGGY_TREE_SERVICE) == "buggy"


def test_analyze_tree_service_detects_fixed() -> None:
    assert setup.analyze_tree_service(FIXED_TREE_SERVICE) == "fixed"


def test_analyze_tree_service_unknown_layout() -> None:
    assert setup.analyze_tree_service("def unrelated():\n    return 1\n") == "unknown"


def test_fix_tree_service_indents_semantic_block() -> None:
    fixed = setup.fix_tree_service(BUGGY_TREE_SERVICE)

    assert fixed == FIXED_TREE_SERVICE
    assert setup.analyze_tree_service(fixed) == "fixed"


def test_fix_tree_service_raises_on_unknown_layout() -> None:
    with pytest.raises(ValueError):
        setup.fix_tree_service("def unrelated():\n    return 1\n")


def test_patch_windows_mcp_tree_patches_then_idempotent(tmp_path: Path) -> None:
    service = tmp_path / "service.py"
    service.write_text(BUGGY_TREE_SERVICE, encoding="utf-8")

    assert setup.patch_windows_mcp_tree(service) == "patched"
    assert service.read_text(encoding="utf-8") == FIXED_TREE_SERVICE
    # Second run: already fixed, content untouched.
    assert setup.patch_windows_mcp_tree(service) == "already_fixed"
    assert service.read_text(encoding="utf-8") == FIXED_TREE_SERVICE


def test_patch_windows_mcp_tree_unknown_layout_untouched(tmp_path: Path) -> None:
    service = tmp_path / "service.py"
    original = "def unrelated():\n    return 1\n"
    service.write_text(original, encoding="utf-8")

    assert setup.patch_windows_mcp_tree(service) == "unknown_layout"
    assert service.read_text(encoding="utf-8") == original


def test_patch_windows_mcp_not_installed() -> None:
    assert setup.patch_windows_mcp(locate=lambda: None) == "not_installed"


class TestEnsureOnnxruntimeDml:
    """setup.py post-install swap: CPU onnxruntime -> onnxruntime-directml."""

    def test_skips_off_windows(self):
        assert setup.ensure_onnxruntime_dml(
            probe=lambda: False, run_cmd=lambda cmd: None, is_windows=False
        ) == "skipped"

    def test_already_ok_when_dml_present(self):
        calls: list[list[str]] = []
        status = setup.ensure_onnxruntime_dml(
            probe=lambda: True, run_cmd=calls.append, is_windows=True
        )
        assert status == "already_ok"
        assert calls == []

    def test_installs_when_dml_missing(self):
        calls: list[list[str]] = []
        status = setup.ensure_onnxruntime_dml(
            probe=lambda: False, run_cmd=calls.append, is_windows=True
        )
        assert status == "installed"
        flat = [" ".join(c) for c in calls]
        assert any("uninstall" in c and "onnxruntime" in c for c in flat)
        assert any("install" in c and "onnxruntime-directml" in c for c in flat)

    def test_pip_failure_is_best_effort(self):
        def boom(cmd: list[str]) -> None:
            raise RuntimeError("pip died")

        assert setup.ensure_onnxruntime_dml(
            probe=lambda: False, run_cmd=boom, is_windows=True
        ) == "failed"

    def test_probe_failure_is_best_effort(self):
        def boom_probe() -> bool:
            raise RuntimeError("no python")

        assert setup.ensure_onnxruntime_dml(
            probe=boom_probe, run_cmd=lambda cmd: None, is_windows=True
        ) == "failed"


# ---------------------------------------------------------------------------
# YOLO (OmniParser icon_detect) weight download
# ---------------------------------------------------------------------------

def _make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _big_model() -> bytes:
    return b"x" * (setup.YOLO_MIN_MODEL_BYTES + 1)


class TestYoloWeightsPresent:
    def test_missing_model(self, tmp_path: Path) -> None:
        assert setup.yolo_weights_present(tmp_path) is False

    def test_too_small_model_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "model.pt").write_bytes(b"tiny")
        assert setup.yolo_weights_present(tmp_path) is False

    def test_real_size_model(self, tmp_path: Path) -> None:
        (tmp_path / "model.pt").write_bytes(_big_model())
        assert setup.yolo_weights_present(tmp_path) is True


class TestDownloadYoloWeights:
    def test_skips_when_already_present(self, tmp_path: Path) -> None:
        target = tmp_path / "icon_detect"
        target.mkdir()
        (target / "model.pt").write_bytes(_big_model())
        calls: list = []

        ok = setup.download_yolo_weights(
            target_dir=target, fetch=lambda url, dest: calls.append((url, dest))
        )

        assert ok is True
        assert calls == []

    def test_extracts_nested_zip_layout(self, tmp_path: Path) -> None:
        target = tmp_path / "icon_detect"
        big = _big_model()

        def fake_fetch(url: str, dest: Path) -> None:
            _make_zip(
                dest,
                {"icon_detect/model.pt": big, "icon_detect/model.yaml": b"cfg"},
            )

        assert setup.download_yolo_weights(target_dir=target, fetch=fake_fetch) is True
        assert (target / "model.pt").read_bytes() == big
        assert (target / "model.yaml").read_bytes() == b"cfg"

    def test_extracts_root_level_zip_layout(self, tmp_path: Path) -> None:
        target = tmp_path / "icon_detect"
        big = _big_model()

        def fake_fetch(url: str, dest: Path) -> None:
            _make_zip(dest, {"model.pt": big})

        assert setup.download_yolo_weights(target_dir=target, fetch=fake_fetch) is True
        assert (target / "model.pt").read_bytes() == big

    def test_fetch_failure_is_best_effort(self, tmp_path: Path) -> None:
        target = tmp_path / "icon_detect"

        def boom(url: str, dest: Path) -> None:
            raise RuntimeError("network down")

        assert setup.download_yolo_weights(target_dir=target, fetch=boom) is False
        assert not (target / "model.pt").exists()

    def test_zip_without_model_is_best_effort(self, tmp_path: Path) -> None:
        target = tmp_path / "icon_detect"

        def fake_fetch(url: str, dest: Path) -> None:
            _make_zip(dest, {"readme.txt": b"no model here"})

        assert setup.download_yolo_weights(target_dir=target, fetch=fake_fetch) is False
        assert not (target / "model.pt").exists()


def test_argparser_keeps_download_weights_but_drops_weights_source() -> None:
    parser = setup.build_argparser()
    args = parser.parse_args(["--download-weights"])
    assert args.download_weights is True
    assert not hasattr(args, "weights_source")
    with pytest.raises(SystemExit):
        parser.parse_args(["--weights-source", "github"])


# ---------------------------------------------------------------------------
# Florence-2 icon_caption weight download (OmniParser icon_caption fine-tune)
# ---------------------------------------------------------------------------

class TestIconCaptionWeightsPresent:
    def test_missing_dir(self, tmp_path: Path) -> None:
        assert setup.icon_caption_weights_present(tmp_path / "icon_caption") is False

    def test_config_without_weights_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{}")
        assert setup.icon_caption_weights_present(tmp_path) is False

    def test_config_and_safetensors(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{}")
        (tmp_path / "model.safetensors").write_bytes(b"x" * 1024)
        assert setup.icon_caption_weights_present(tmp_path) is True


class TestDownloadIconCaptionWeights:
    def test_skips_when_already_present(self, tmp_path: Path) -> None:
        target = tmp_path / "icon_caption"
        target.mkdir()
        (target / "config.json").write_text("{}")
        (target / "model.safetensors").write_bytes(b"x" * 1024)
        calls: list = []

        ok = setup.download_icon_caption_weights(
            target_dir=target,
            snapshot_download=lambda *a, **k: calls.append((a, k)),
        )

        assert ok is True
        assert calls == []

    def test_downloads_snapshot_and_copies_subfolder(self, tmp_path: Path) -> None:
        target = tmp_path / "icon_caption"
        calls: list[dict] = []

        def fake_snapshot(repo, allow_patterns, local_dir=None):
            calls.append(
                {"repo": repo, "allow_patterns": allow_patterns, "local_dir": local_dir}
            )
            if local_dir is None:
                return None  # processor warm-up: goes to the HF cache
            src = Path(local_dir) / "icon_caption"
            src.mkdir(parents=True)
            (src / "config.json").write_text("{}")
            (src / "model.safetensors").write_bytes(b"w" * 1024)
            return local_dir

        ok = setup.download_icon_caption_weights(
            target_dir=target, snapshot_download=fake_snapshot
        )

        assert ok is True
        assert calls[0]["repo"] == setup.ICON_CAPTION_REPO
        assert calls[0]["allow_patterns"] == "icon_caption/*"
        assert calls[1]["repo"] == setup.ICON_CAPTION_PROCESSOR_REPO
        assert calls[1]["local_dir"] is None
        assert (target / "config.json").exists()
        assert (target / "model.safetensors").read_bytes() == b"w" * 1024

    def test_processor_warmup_failure_still_succeeds(self, tmp_path: Path) -> None:
        """The main weights matter; a failed processor warm-up only warns —
        the first caption fetches the processor lazily."""
        target = tmp_path / "icon_caption"

        def fake_snapshot(repo, allow_patterns, local_dir=None):
            if local_dir is None:
                raise RuntimeError("processor repo unreachable")
            src = Path(local_dir) / "icon_caption"
            src.mkdir(parents=True)
            (src / "config.json").write_text("{}")
            (src / "model.safetensors").write_bytes(b"w" * 1024)
            return local_dir

        ok = setup.download_icon_caption_weights(
            target_dir=target, snapshot_download=fake_snapshot
        )

        assert ok is True
        assert (target / "model.safetensors").exists()

    def test_download_failure_is_best_effort(self, tmp_path: Path) -> None:
        target = tmp_path / "icon_caption"

        def boom(*a, **k):
            raise RuntimeError("network down")

        assert (
            setup.download_icon_caption_weights(
                target_dir=target, snapshot_download=boom
            )
            is False
        )
        assert not (target / "config.json").exists()

    def test_snapshot_without_subfolder_is_best_effort(self, tmp_path: Path) -> None:
        target = tmp_path / "icon_caption"

        def fake_snapshot(repo, allow_patterns, local_dir):
            return local_dir  # nothing downloaded

        assert (
            setup.download_icon_caption_weights(
                target_dir=target, snapshot_download=fake_snapshot
            )
            is False
        )
