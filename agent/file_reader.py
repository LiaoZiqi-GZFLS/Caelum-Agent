"""Read binary documents (PDF/DOCX/PPTX/EPUB/...) via the Kimi Files API.

The Files API's ``file-extract`` purpose parses complex binary formats that the
filesystem MCP server can only return as garbage bytes. This module wraps the
upload → fetch-content → delete flow behind a single :class:`FileExtractor`
with a local sha256 cache, and exposes it to the model as the ``ReadDocument``
local function tool with character-based pagination so a 200-page PDF cannot
blow up the context window in one tool result.

Scope is deliberately narrow: plain text, code, logs, and spreadsheets stay
with the filesystem MCP / ``moonshot/excel`` tools (local, free, no upload).
``ReadDocument`` is for binary documents only — hence the extension allowlist.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from agent.config import LLMConfig

logger = logging.getLogger("caelum.file_reader")

# Only formats the local stack cannot parse itself. Text-like formats (.txt,
# .md, .csv, code, logs) are intentionally excluded: the filesystem MCP reads
# them locally at zero cost and zero upload.
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".epub",
    ".mobi",
    ".xls",
    ".xlsx",
}

DEFAULT_PAGE_CHARS = 8000
MAX_PAGE_CHARS = 16000


class FileExtractor:
    """Upload a document to the Kimi Files API and return its extracted text.

    Extracted text is cached under ``cache_dir/<sha256>.txt`` so repeated reads
    of the same file cost no upload round-trip. The remote copy is deleted
    after extraction to stay within the 1000-file / 10GB account quota.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        cache_dir: Path | str,
        http: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        # When no client is injected (tests inject a fake), share nothing and
        # own a private one — but production passes llm.http to reuse the pool.
        self.http = http if http is not None else httpx.AsyncClient(timeout=120.0)

    def _cache_path(self, path: Path) -> Path:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return self.cache_dir / f"{digest}.txt"

    def ref_for(self, path: str | Path) -> str:
        """Return the ``doc:<sha8>`` handle for a document's cached extraction.

        The handle lets other tools (DraftContent) pull the extracted text
        straight from the local cache without the main context ever seeing it.
        """
        digest = hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()
        return f"doc:{digest[:8]}"

    def read_by_ref(self, ref: str) -> str:
        """Resolve a ``doc:<sha8>`` handle back to the cached extracted text."""
        match = re.fullmatch(r"doc:([0-9a-f]{8})", ref or "")
        if not match:
            raise ValueError(
                f"Invalid doc_ref '{ref}'. Use the ref returned by ReadDocument."
            )
        matches = list(self.cache_dir.glob(f"{match.group(1)}*.txt"))
        if not matches:
            raise ValueError(
                f"Unknown doc_ref '{ref}': the document has not been read with "
                "ReadDocument (or its cache was cleared)."
            )
        return matches[0].read_text(encoding="utf-8")

    async def extract(self, path: str | Path) -> str:
        """Return the extracted text of a supported document.

        Raises FileNotFoundError for missing files and ValueError for
        unsupported extensions; the tool handler formats these as "[error]".
        """
        p = Path(path).expanduser()
        if p.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type '{p.suffix}'. ReadDocument only handles "
                f"binary documents ({', '.join(sorted(ALLOWED_EXTENSIONS))}). "
                "Read text/code/log files with the Filesystem tools instead."
            )
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {p}")
        cache = self._cache_path(p)
        if cache.exists():
            logger.info("file-extract cache hit: %s", p.name)
            return cache.read_text(encoding="utf-8")
        text = await self._upload_and_extract(p)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return text

    async def sweep_remote(self) -> int:
        """Delete all leftover file-extract uploads on the account.

        The platform keeps uploaded files indefinitely against the 1000-file /
        10GB quota, and our per-read delete is only best-effort — so leaked
        files accumulate forever. file-extract uploads are throwaway by design
        (the extracted text is cached locally by sha256), so deleting every
        one of them is safe. Returns the number of files deleted; never raises.
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = await self.http.get(f"{self.base_url}/files", headers=headers)
            resp.raise_for_status()
            files = resp.json().get("data", [])
        except Exception as exc:
            logger.warning("File sweep: listing remote files failed: %s", exc)
            return 0
        deleted = 0
        for entry in files:
            if entry.get("purpose") != "file-extract":
                continue
            try:
                await self.http.delete(
                    f"{self.base_url}/files/{entry['id']}", headers=headers
                )
                deleted += 1
            except Exception as exc:
                logger.warning(
                    "File sweep: failed to delete %s: %s", entry.get("id"), exc
                )
        if deleted:
            logger.info(
                "File sweep: deleted %d leftover file-extract upload(s)", deleted
            )
        return deleted

    async def _upload_and_extract(self, path: Path) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with path.open("rb") as fh:
            resp = await self.http.post(
                f"{self.base_url}/files",
                headers=headers,
                data={"purpose": "file-extract"},
                files={"file": (path.name, fh)},
            )
        resp.raise_for_status()
        file_id = resp.json()["id"]
        try:
            content = await self.http.get(
                f"{self.base_url}/files/{file_id}/content", headers=headers
            )
            content.raise_for_status()
            return content.text
        finally:
            # Best-effort quota hygiene: a failed delete must not fail the read.
            try:
                await self.http.delete(f"{self.base_url}/files/{file_id}", headers=headers)
            except Exception as exc:
                logger.warning("Failed to delete remote file %s: %s", file_id, exc)


def make_read_document_handler(extractor: FileExtractor):
    """Build the async ReadDocument tool handler with char-based pagination."""

    async def read_document(
        path: str, offset: int = 0, limit: int = DEFAULT_PAGE_CHARS
    ) -> str:
        try:
            text = await extractor.extract(path)
        except Exception as exc:
            return f"[error] {exc}"
        total = len(text)
        offset = max(0, int(offset))
        limit = min(max(1, int(limit)), MAX_PAGE_CHARS)
        page = text[offset : offset + limit]
        end = offset + len(page)
        header = (
            f"[read_document] {Path(path).name}: {total} chars total, "
            f"showing {offset}-{end} "
            f"(ref {extractor.ref_for(path)} — pass to DraftContent as "
            "doc_ref to write from this document without loading it here)"
        )
        if end < total:
            page += (
                f"\n[truncated] Call ReadDocument again with offset={end} "
                "for the next page."
            )
        return header + "\n" + page

    return read_document


READ_DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to a local binary document (.pdf, .docx, .pptx, .epub, .xlsx, ...).",
        },
        "offset": {
            "type": "integer",
            "description": "Character offset to start reading from, for pagination. Default 0.",
            "default": 0,
        },
        "limit": {
            "type": "integer",
            "description": f"Max characters to return (default {DEFAULT_PAGE_CHARS}, max {MAX_PAGE_CHARS}).",
            "default": DEFAULT_PAGE_CHARS,
        },
    },
    "required": ["path"],
}


def register_read_document(
    llm: Any, config: LLMConfig, cache_dir: Path | str
) -> FileExtractor | None:
    """Register the ReadDocument local function tool, if enabled in config.

    Returns the FileExtractor so the caller can schedule a remote sweep; None
    when the feature is disabled.
    """
    if not config.enable_file_extract:
        return None
    # Reuse the LLM client's httpx pool when available (production); the tool
    # tests inject their own extractor so this path only wires wiring.
    http = getattr(llm, "http", None)
    extractor = FileExtractor(
        base_url=config.base_url,
        api_key=config.api_key,
        cache_dir=Path(cache_dir) / "file_extract",
        http=http,
    )
    llm.register_local_function(
        "ReadDocument",
        make_read_document_handler(extractor),
        schema=READ_DOCUMENT_SCHEMA,
        description=(
            "Extract and read the text of a BINARY document (PDF, Word, "
            "PowerPoint, EPUB, Excel) via the Kimi Files API. Use this only "
            "for binary documents; read plain text/code/log files with the "
            "Filesystem tools instead. Long documents are paginated: re-call "
            "with the suggested offset to continue."
        ),
    )
    return extractor
