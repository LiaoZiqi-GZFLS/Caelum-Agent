"""Resume-friendly downloader for GUI-Actor-3B weights from hf-mirror.com."""

from __future__ import annotations

import sys
from pathlib import Path

# huggingface_hub reads HF_ENDPOINT lazily from constants; set it before importing
# the download functions to ensure requests go through the China mirror.
from huggingface_hub import constants

constants.HF_ENDPOINT = "https://hf-mirror.com"

from huggingface_hub import hf_hub_download

REPO_ID = "microsoft/GUI-Actor-3B-Qwen2.5-VL"
LOCAL_DIR = Path("models/gui-actor-3b")
FILES = [
    ".gitattributes",
    "README.md",
    "added_tokens.json",
    "chat_template.json",
    "config.json",
    "generation_config.json",
    "merges.txt",
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
    "model.safetensors.index.json",
    "preprocessor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "trainer_state.json",
    "vocab.json",
]


def main() -> int:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    for filename in FILES:
        print(f"Downloading {filename} ...")
        try:
            path = hf_hub_download(
                repo_id=REPO_ID,
                filename=filename,
                local_dir=str(LOCAL_DIR),
                local_dir_use_symlinks=False,
                resume_download=True,
            )
            print(f"  -> {path}")
        except Exception as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            return 1
    print("\nAll downloads complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
