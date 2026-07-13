"""Pydantic configuration for Caelum-Agent.

Loads config.yaml and exposes typed settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMConfig(BaseModel):
    model_config = ConfigDict(repr=False)

    provider: str = "kimi"
    base_url: str = "https://api.moonshot.cn/v1"
    api_key: str = Field(repr=False)
    model: str = "kimi-k2.6"
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    enable_builtin_tools: bool = True
    builtin_tools: list[str] = Field(default_factory=list)
    # Register the ReadDocument tool (Kimi Files API file-extract) for binary
    # documents (PDF/DOCX/PPTX/EPUB/XLSX). Disable to keep documents local-only.
    enable_file_extract: bool = True
    # Register the ViewMedia tool (Kimi Files API image/video upload with
    # native ms:// rendering). Disable to keep local media files offline.
    enable_media_upload: bool = True

    @field_validator("model")
    @classmethod
    def normalize_model_name(cls, v: str) -> str:
        if v == "kimi-k2-6":
            return "kimi-k2.6"
        return v


class MCPServerConfig(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class MCPConfig(BaseModel):
    playwright: MCPServerConfig | None = None
    windows: MCPServerConfig | None = None
    filesystem: MCPServerConfig | None = None


class IconCaptionConfig(BaseModel):
    # Florence-2 icon captioning (OmniParser icon_caption fine-tune): gives
    # YOLO-detected icons a semantic description ("magnifier", "red close
    # button") so bare icon markers carry content like OCR text markers do.
    enabled: bool = True
    model_path: str = "./models/omniparser/icon_caption"
    # Processor source: the icon_caption checkpoint ships no processor files.
    # setup.py --download-weights installs them here from the GitHub Release
    # mirror; when this dir is missing the captioner falls back to the
    # microsoft/Florence-2-base-ft HF repo at load time.
    processor_path: str = "./models/omniparser/icon_caption_processor"
    # Inference device; automatically falls back to cpu once if a cuda
    # generate call raises.
    device: str = "cuda:0"
    # Only bare icon markers (no OCR text after fusion) get captioned, capped
    # per frame (highest score first) to bound latency.
    max_icons: int = 30
    batch_size: int = 8
    max_new_tokens: int = 20


class YoloConfig(BaseModel):
    # OmniParser icon_detect YOLOv8 for SoM annotation when the UIA tree is
    # unavailable (WeChat/Qt/Electron): the vision grounding backend.
    enabled: bool = True
    model_path: str = "./models/omniparser/icon_detect/model.pt"
    # Inference device; automatically falls back to cpu once if a cuda
    # predict call raises.
    device: str = "cuda:0"
    conf: float = 0.25
    imgsz: int = 1280
    # Run YOLO on any perception frame whose UI tree is empty but OCR found
    # text, so the model gets clickable SoM markers without asking.
    auto_compensate: bool = True


class ScreenshotConfig(BaseModel):
    backend: Literal["mss", "PIL"] = "mss"
    # The model-facing screenshot uses the same inverse-DPI normalization as
    # OCR (original at 100% scale, 1/scale above, floored at a 1080p box —
    # see perception._ocr_resize_ratio); UpgradeVision switches to the
    # original image. There are no size knobs on purpose.
    quality: int = 60
    format: Literal["JPEG", "PNG"] = "JPEG"
    crop_to_active_window: bool = False


class OCRConfig(BaseModel):
    enabled: bool = True
    backend: str = "rapidocr_onnxruntime"
    # DirectML (GPU) inference for all three OCR models. Requires the
    # onnxruntime-directml package (setup.py installs it on Windows, replacing
    # CPU onnxruntime); rapidocr falls back to CPU with a warning when the
    # DmlExecutionProvider is unavailable, so this is safe to leave on.
    use_dml: bool = True


class MemoryConfig(BaseModel):
    sqlite_path: str = "./data/memory.db"
    use_kimi_memory: bool = True


class ReflectionConfig(BaseModel):
    use_rethink: bool = True


class SecurityConfig(BaseModel):
    default_level: Literal["read", "write_safe", "write_risky", "destructive"] = "read"
    auto_execute_levels: list[str] = Field(default_factory=lambda: ["read", "write_safe"])
    confirm_levels: list[str] = Field(default_factory=lambda: ["write_risky"])
    destructive_requires_approval: bool = True
    # When True, destructive actions require the user to type the action summary
    # to confirm, not just a y/n prompt.
    destructive_requires_typed_confirmation: bool = True


class SkillConfig(BaseModel):
    # Cosine similarity threshold for merging a new trace into an existing skill.
    similarity_threshold: float = 0.85
    # Directory for auto-learned skills, relative to skills_dir.
    learned_subdir: str = "learned"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} | {message}"
    data_dir: str = "./data"


class PathsConfig(BaseModel):
    skills_dir: str = "./skills"
    cache_dir: str = "./data/cache"
    audit_log: str = "./data/audit.log"


class KillSwitchConfig(BaseModel):
    enabled: bool = True
    api_failure_threshold: int = 5
    action_failure_threshold: int = 3
    same_ui_loop_threshold: int = 3


class Config(BaseModel):
    llm: LLMConfig
    mcp_servers: MCPConfig = Field(default_factory=MCPConfig)
    yolo: YoloConfig = Field(default_factory=YoloConfig)
    icon_caption: IconCaptionConfig = Field(default_factory=IconCaptionConfig)
    screenshot: ScreenshotConfig = Field(default_factory=ScreenshotConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    reflection: ReflectionConfig = Field(default_factory=ReflectionConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    skills: SkillConfig = Field(default_factory=SkillConfig)
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def sqlite_path_absolute(self) -> Path:
        return Path(self.memory.sqlite_path).expanduser().resolve()

    def cache_dir_absolute(self) -> Path:
        return Path(self.paths.cache_dir).expanduser().resolve()

    def skills_dir_absolute(self) -> Path:
        return Path(self.paths.skills_dir).expanduser().resolve()

    def learned_skills_dir_absolute(self) -> Path:
        return self.skills_dir_absolute() / self.skills.learned_subdir

    def audit_log_absolute(self) -> Path:
        return Path(self.paths.audit_log).expanduser().resolve()


def load_config(path: Path | str | None = None) -> Config:
    if path is None:
        path = Path(__file__).resolve().parents[1] / "config.yaml"
    return Config.from_yaml(path)
