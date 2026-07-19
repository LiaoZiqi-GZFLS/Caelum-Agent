"""DraftContent: a content-writing subagent invoked as a tool.

Some user requests are content tasks (write a Zhihu article, draft an email)
rather than UI-operation tasks. Running them in the main agent pollutes its
context with drafting iterations and mixes the writer persona into the
UI-operator system prompt. DraftContent spawns a single-shot "subagent": a
separate Kimi chat call with its own system prompt (the persona), no tools,
and an optional Kimi Partial Mode prefill so the caller controls the opening
(title, fixed greeting, or text to continue from).

The draft is written to ``<drafts_dir>/<slug>-<hash>.md`` and the tool result
returns only the path, size, and a short preview — the full text never enters
the main agent's context unless it chooses to read the file. To paste the
draft into an editor, pipe the file to the clipboard with PowerShell instead
of typing it through the agent.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("caelum.content_writer")

DEFAULT_MAX_CHARS = 4000
HARD_MAX_CHARS = 20000
MAX_DOC_CHARS = 60000  # cap on document text injected into the subagent
_PREVIEW_CHARS = 200


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text).strip().lower()
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug[:40] or "draft"


def make_draft_content_handler(
    llm: Any,
    drafts_dir: Path | str,
    doc_resolver: Any | None = None,
):
    """Build the async DraftContent tool handler.

    ``llm`` is the main LLMClient reused for a detached, tool-free chat call
    (same API credentials, fresh message list). ``doc_resolver`` resolves a
    ``doc:<sha8>`` ref (from ReadDocument) to the document's extracted text;
    when provided, the text is injected into the subagent's context so the
    main agent never has to load it.
    """
    drafts = Path(drafts_dir)

    async def draft_content(
        task: str,
        persona: str,
        prefill: str = "",
        max_chars: int = DEFAULT_MAX_CHARS,
        doc_ref: str | None = None,
    ) -> str:
        task = (task or "").strip()
        if not task:
            return "[error] DraftContent requires a non-empty task."
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": persona or "You are a professional writer."},
        ]
        if doc_ref:
            if doc_resolver is None:
                return (
                    "[error] doc_ref requires ReadDocument to be enabled; "
                    "read the document with ReadDocument first."
                )
            try:
                doc_text = doc_resolver(doc_ref)
            except Exception as exc:
                return f"[error] {exc}"
            if len(doc_text) > MAX_DOC_CHARS:
                doc_text = doc_text[:MAX_DOC_CHARS] + "\n[document truncated]"
            # Kimi's file-chat pattern: document content as its own system
            # message, between the persona and the task.
            messages.append(
                {"role": "system", "content": f"Reference document:\n{doc_text}"}
            )
        messages.append({"role": "user", "content": task})
        if prefill:
            # Kimi Partial Mode: the model CONTINUES the prefilled text, and
            # the API response excludes it — concatenate it back. Must not be
            # combined with response_format.
            messages.append(
                {"role": "assistant", "content": prefill, "partial": True}
            )
        try:
            completion = await llm.chat(messages, tool_choice="none")
        except Exception as exc:
            logger.warning("DraftContent subagent failed: %s", exc)
            return f"[error] DraftContent generation failed: {exc}"
        body = (completion.choices[0].message.content or "").strip()
        text = prefill + body
        limit = min(max(1, int(max_chars)), HARD_MAX_CHARS)
        truncated = len(text) > limit
        if truncated:
            text = text[:limit]

        drafts.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        path = drafts / f"{_slug(task)}-{digest}.md"
        path.write_text(text, encoding="utf-8")
        logger.info("DraftContent wrote %s (%d chars)", path, len(text))

        result = (
            f"[draft] Wrote {path} ({len(text)} chars)"
            + (" [truncated at max_chars]" if truncated else "")
            + f"\nPreview:\n{text[:_PREVIEW_CHARS]}"
            + (
                "\n\nTo paste into an editor without loading the text into "
                f"context: Get-Content '{path}' | Set-Clipboard (then Ctrl+V)."
            )
        )
        return result

    return draft_content


DRAFT_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "What to write, including topic, length, audience, and style requirements.",
        },
        "persona": {
            "type": "string",
            "description": "The writer persona used as the subagent's system prompt (e.g. 'senior tech columnist, calm and precise').",
        },
        "prefill": {
            "type": "string",
            "description": "Optional opening the draft must continue from (title, fixed greeting, or existing text to extend). Uses Kimi Partial Mode.",
            "default": "",
        },
        "max_chars": {
            "type": "integer",
            "description": f"Hard cap on the draft length in characters (default {DEFAULT_MAX_CHARS}, max {HARD_MAX_CHARS}).",
            "default": DEFAULT_MAX_CHARS,
        },
        "doc_ref": {
            "type": "string",
            "description": "Optional doc:<sha8> reference returned by ReadDocument. Injects the document's full extracted text into the writer subagent without loading it into the main context.",
        },
    },
    "required": ["task", "persona"],
}


def register_draft_content(
    llm: Any, drafts_dir: Path | str, doc_resolver: Any | None = None
) -> None:
    """Register the DraftContent local function tool."""
    llm.register_local_function(
        "DraftContent",
        make_draft_content_handler(llm, drafts_dir, doc_resolver=doc_resolver),
        schema=DRAFT_CONTENT_SCHEMA,
        description=(
            "Generate long-form content (article, email, post) in a detached "
            "writer subagent with its own persona, keeping the main context "
            "clean. The draft is saved to a file; the result returns only the "
            "path, size, and a short preview. Use 'prefill' to force the "
            "opening or to continue existing text (Kimi Partial Mode), and "
            "'doc_ref' (from ReadDocument) to write from a document without "
            "loading its text into the main context. Paste the draft via the "
            "clipboard hint instead of re-reading it."
        ),
    )
