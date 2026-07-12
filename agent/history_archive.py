"""Flight-recorder archive of task histories.

Append-only, never read back by the agent: one JSONL file per task under
``data/archives/``, written when run_task finishes (success or failure).
The first line is a metadata record; every following line is one history
message. Base64 screenshots are replaced by a placeholder (the files remain
in data/cache/), and tool-call arguments are redacted with the same sensitive
key set used by the audit log.

This is deliberately separate from state persistence: the archive is for
post-hoc review and evaluation evidence, never for restoring context.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("caelum.history_archive")

_IMAGE_PLACEHOLDER = {"type": "text", "text": "[screenshot omitted from archive]"}


class HistoryArchiver:
    def __init__(
        self,
        archives_dir: Path | str,
        sensitive_keys: frozenset[str] = frozenset(),
    ) -> None:
        self.archives_dir = Path(archives_dir)
        self.sensitive_keys = sensitive_keys

    def archive(
        self,
        task_id: str | None,
        instruction: str,
        outcome: str,
        history: list[dict[str, Any]],
    ) -> Path | None:
        """Write one JSONL archive file; never raises (returns None on failure)."""
        try:
            self.archives_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safe_id = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in (task_id or "notask")
            )[:40]
            path = self.archives_dir / f"{stamp}-{safe_id}.jsonl"
            records = [
                {
                    "type": "metadata",
                    "task_id": task_id,
                    "instruction": instruction,
                    "outcome": outcome,
                    "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            ]
            records.extend(self._sanitize(m) for m in history)
            path.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
                + "\n",
                encoding="utf-8",
            )
            logger.info("History archived to %s", path)
            return path
        except Exception as exc:
            logger.warning("History archive failed: %s", exc)
            return None

    def _sanitize(self, message: dict[str, Any]) -> dict[str, Any]:
        msg = dict(message)
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [
                _IMAGE_PLACEHOLDER
                if isinstance(part, dict)
                and part.get("type") == "image_url"
                and str(part.get("image_url", {}).get("url", "")).startswith("data:")
                else part
                for part in content
            ]
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            msg["tool_calls"] = [self._redact_call(c) for c in tool_calls]
        return msg

    def _redact_call(self, call: dict[str, Any]) -> dict[str, Any]:
        call = json.loads(json.dumps(call, default=str))
        func = call.get("function", {})
        raw = func.get("arguments")
        if isinstance(raw, str) and self.sensitive_keys:
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return call
            if isinstance(args, dict):
                func["arguments"] = json.dumps(
                    {
                        k: ("***" if k.lower() in self.sensitive_keys else v)
                        for k, v in args.items()
                    },
                    ensure_ascii=False,
                )
        return call
