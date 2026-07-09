"""Pydantic configuration for Caelum-Agent.

Loads config.yaml and exposes typed settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class LLMConfig(BaseModel):
    provider: str = "kimi"
    base_url: str = "https://api.moonshot.cn/v1"
    api_key: str
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
    playwright: MCPServerConfig
    windows: MCPServerConfig
    filesystem: MCPServerConfig


class UIDetectorConfig(BaseModel):
    enabled: bool = True
    model_path: str = "./models/gui-actor-3b"
    device: str = "cuda:0"
    dtype: Literal["bfloat16", "float16", "float32"] = "bfloat16"
    attn_implementation: Literal["flash_attention_2", "sdpa", "eager"] = "sdpa"
    topk: int = 3
    verifier: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})


class ScreenshotConfig(BaseModel):
    backend: Literal["mss", "PIL"] = "mss"
    max_width: int = 800
    max_height: int = 600
    quality: int = 60
    format: Literal["JPEG", "PNG"] = "JPEG"
    crop_to_active_window: bool = False


class OCRConfig(BaseModel):
    enabled: bool = True
    backend: str = "rapidocr_onnxruntime"


class MemoryConfig(BaseModel):
    sqlite_path: str = "./data/memory.db"


class SecurityConfig(BaseModel):
    default_level: Literal["read", "write_safe", "write_risky", "destructive"] = "read"
    auto_execute_levels: list[str] = Field(default_factory=lambda: ["read", "write_safe"])
    confirm_levels: list[str] = Field(default_factory=lambda: ["write_risky"])
    destructive_requires_approval: bool = True


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
    mcp_servers: MCPConfig
    ui_detector: UIDetectorConfig = Field(default_factory=UIDetectorConfig)
    screenshot: ScreenshotConfig = Field(default_factory=ScreenshotConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
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


def load_config(path: Path | str | None = None) -> Config:
    if path is None:
        path = Path(__file__).resolve().parents[1] / "config.yaml"
    return Config.from_yaml(path)
