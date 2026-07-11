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


class UIDetectorConfig(BaseModel):
    enabled: bool = True
    lazy: bool = True
    # When True together with lazy=True, load the model at startup (warm) but keep
    # on-demand inference: perceive() still skips annotate, and vision runs only
    # when DesktopInteract needs SoM coordinates. Trades resident GPU/CPU memory
    # for eliminating the first-click load stall. Defaults to True (warm start);
    # set to False to defer the load to the first DesktopInteract. Ignored when
    # lazy=False (eager mode already loads at startup and annotates every
    # perception).
    preload: bool = True
    model_path: str = "./models/gui-actor-3b"
    device: str = "cuda:0"
    dtype: Literal["bfloat16", "float16", "float32"] = "bfloat16"
    attn_implementation: Literal["flash_attention_2", "sdpa", "eager"] = "sdpa"
    topk: int = 3
    verifier: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})


class ScreenshotConfig(BaseModel):
    backend: Literal["mss", "PIL"] = "mss"
    max_width: int = 1280
    max_height: int = 720
    quality: int = 60
    format: Literal["JPEG", "PNG"] = "JPEG"
    crop_to_active_window: bool = False


class OCRConfig(BaseModel):
    enabled: bool = True
    backend: str = "rapidocr_onnxruntime"


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
    ui_detector: UIDetectorConfig = Field(default_factory=UIDetectorConfig)
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

    def model_path_absolute(self) -> Path:
        return Path(self.ui_detector.model_path).expanduser().resolve()

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
