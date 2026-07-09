"""Smoke test: load GUI-Actor-3B model architecture without full inference."""

from pathlib import Path

from transformers import AutoConfig

from agent.config import UIDetectorConfig
from ui_detector import UIDetector


def main() -> int:
    model_path = Path("./models/gui-actor-3b").resolve()
    if not model_path.exists():
        print(f"Model path not found: {model_path}")
        return 1

    print(f"Loading config from {model_path}...")
    config = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
    print("Config architecture:", config.architectures)

    print("Initializing UIDetector (this may take a while)...")
    detector = UIDetector(
        UIDetectorConfig(
            model_path=str(model_path),
            device="cpu",
            dtype="float32",
            attn_implementation="eager",
            topk=1,
            verifier={"enabled": False},
        )
    )
    detector.load()
    print("GUI-Actor-3B loaded successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
