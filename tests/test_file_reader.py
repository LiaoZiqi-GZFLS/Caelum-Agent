"""Tests for the Kimi Files API document reader (agent/file_reader.py)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from agent.config import LLMConfig
from agent.file_reader import (
    ALLOWED_EXTENSIONS,
    DEFAULT_PAGE_CHARS,
    MAX_PAGE_CHARS,
    FileExtractor,
    make_read_document_handler,
    register_read_document,
)


class _FakeResponse:
    def __init__(self, json_data: dict[str, Any] | None = None, text: str = "") -> None:
        self._json = json_data or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        pass


class _FakeHTTP:
    """Minimal async stand-in for httpx.AsyncClient used by FileExtractor."""

    def __init__(self, content: str = "extracted text") -> None:
        self.content = content
        self.posts: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.deletes: list[str] = []
        self.list_payload: dict[str, Any] = {"data": []}
        self.fail_post: Exception | None = None
        self.fail_get: Exception | None = None
        self.fail_delete: Exception | None = None
        self.fail_delete_ids: set[str] = set()

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        if self.fail_post is not None:
            raise self.fail_post
        self.posts.append({"url": url, **kwargs})
        return _FakeResponse({"id": "file-abc123"})

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        if self.fail_get is not None:
            raise self.fail_get
        self.gets.append(url)
        if url.endswith("/files"):
            return _FakeResponse(json_data=self.list_payload)
        return _FakeResponse(text=self.content)

    async def delete(self, url: str, **kwargs: Any) -> _FakeResponse:
        if self.fail_delete is not None:
            raise self.fail_delete
        if any(bad in url for bad in self.fail_delete_ids):
            raise RuntimeError("delete failed")
        self.deletes.append(url)
        return _FakeResponse({"deleted": True})


def _extractor(tmp_path: Path, http: _FakeHTTP | None = None) -> FileExtractor:
    return FileExtractor(
        base_url="https://api.moonshot.cn/v1",
        api_key="sk-test",
        cache_dir=tmp_path / "file_extract",
        http=http or _FakeHTTP(),
    )


@pytest.mark.asyncio
async def test_extract_uploads_fetches_deletes_and_caches(tmp_path: Path) -> None:
    http = _FakeHTTP(content="hello from pdf")
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4 fake bytes")
    extractor = _extractor(tmp_path, http)

    text = await extractor.extract(doc)

    assert text == "hello from pdf"
    # Upload hit the Files endpoint with the right purpose and auth header.
    assert len(http.posts) == 1
    post = http.posts[0]
    assert post["url"].endswith("/files")
    assert post["data"] == {"purpose": "file-extract"}
    assert post["headers"]["Authorization"] == "Bearer sk-test"
    assert post["files"]["file"][0] == "report.pdf"
    # Content fetched and remote copy deleted (quota hygiene).
    assert http.gets == ["https://api.moonshot.cn/v1/files/file-abc123/content"]
    assert http.deletes == ["https://api.moonshot.cn/v1/files/file-abc123"]
    # Cached locally under the file's sha256.
    digest = hashlib.sha256(b"%PDF-1.4 fake bytes").hexdigest()
    assert (tmp_path / "file_extract" / f"{digest}.txt").read_text(encoding="utf-8") == "hello from pdf"


@pytest.mark.asyncio
async def test_extract_uses_cache_without_http(tmp_path: Path) -> None:
    http = _FakeHTTP()
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"same bytes")
    extractor = _extractor(tmp_path, http)

    first = await extractor.extract(doc)
    second = await extractor.extract(doc)

    assert first == second == "extracted text"
    assert len(http.posts) == 1  # second call served from cache


@pytest.mark.asyncio
async def test_extract_rejects_unsupported_extension(tmp_path: Path) -> None:
    http = _FakeHTTP()
    doc = tmp_path / "script.py"
    doc.write_text("print('hi')", encoding="utf-8")
    extractor = _extractor(tmp_path, http)

    with pytest.raises(ValueError, match="Unsupported"):
        await extractor.extract(doc)
    assert http.posts == []


@pytest.mark.asyncio
async def test_extract_missing_file_raises(tmp_path: Path) -> None:
    extractor = _extractor(tmp_path)
    with pytest.raises(FileNotFoundError):
        await extractor.extract(tmp_path / "nope.pdf")


@pytest.mark.asyncio
async def test_extract_delete_failure_is_tolerated(tmp_path: Path) -> None:
    http = _FakeHTTP(content="text")
    http.fail_delete = RuntimeError("quota cleanup failed")
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"bytes")
    extractor = _extractor(tmp_path, http)

    text = await extractor.extract(doc)  # must not raise

    assert text == "text"


@pytest.mark.asyncio
async def test_handler_returns_error_string_on_upload_failure(tmp_path: Path) -> None:
    http = _FakeHTTP()
    http.fail_post = RuntimeError("network down")
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"bytes")
    handler = make_read_document_handler(_extractor(tmp_path, http))

    result = await handler(path=str(doc))

    assert result.startswith("[error]")
    assert "network down" in result


@pytest.mark.asyncio
async def test_handler_rejects_text_files_with_guidance(tmp_path: Path) -> None:
    doc = tmp_path / "notes.txt"
    doc.write_text("plain text", encoding="utf-8")
    handler = make_read_document_handler(_extractor(tmp_path))

    result = await handler(path=str(doc))

    assert result.startswith("[error]")
    assert "Filesystem" in result or "filesystem" in result


@pytest.mark.asyncio
async def test_handler_paginates_long_documents(tmp_path: Path) -> None:
    long_text = "A" * (DEFAULT_PAGE_CHARS + 500)
    http = _FakeHTTP(content=long_text)
    doc = tmp_path / "big.pdf"
    doc.write_bytes(b"bytes")
    handler = make_read_document_handler(_extractor(tmp_path, http))

    page1 = await handler(path=str(doc))
    page2 = await handler(path=str(doc), offset=DEFAULT_PAGE_CHARS)

    assert f"0-{DEFAULT_PAGE_CHARS}" in page1
    assert f"offset={DEFAULT_PAGE_CHARS}" in page1  # continuation hint
    assert "truncated" not in page2
    assert page2.rstrip().endswith("A" * 100)  # tail of the document


@pytest.mark.asyncio
async def test_handler_caps_limit(tmp_path: Path) -> None:
    http = _FakeHTTP(content="B" * (MAX_PAGE_CHARS * 3))
    doc = tmp_path / "big.pdf"
    doc.write_bytes(b"bytes")
    handler = make_read_document_handler(_extractor(tmp_path, http))

    page = await handler(path=str(doc), limit=MAX_PAGE_CHARS * 2)

    assert f"0-{MAX_PAGE_CHARS}" in page  # clamped to MAX_PAGE_CHARS


def test_allowed_extensions_are_binary_documents() -> None:
    assert {".pdf", ".docx", ".pptx", ".epub", ".xlsx"} <= ALLOWED_EXTENSIONS
    assert ".txt" not in ALLOWED_EXTENSIONS
    assert ".py" not in ALLOWED_EXTENSIONS


class _RecordingLLM:
    def __init__(self) -> None:
        self.registered: dict[str, dict[str, Any]] = {}

    def register_local_function(
        self, name: str, handler: Any, schema: dict[str, Any], description: str
    ) -> None:
        self.registered[name] = {"handler": handler, "schema": schema, "description": description}


def test_register_read_document_registers_tool_when_enabled(tmp_path: Path) -> None:
    llm = _RecordingLLM()
    config = LLMConfig(api_key="sk-test", enable_file_extract=True)

    extractor = register_read_document(llm, config, tmp_path / "cache")

    assert "ReadDocument" in llm.registered
    schema = llm.registered["ReadDocument"]["schema"]
    assert set(schema["properties"]) >= {"path", "offset", "limit"}
    assert schema["required"] == ["path"]
    assert isinstance(extractor, FileExtractor)


def test_register_read_document_skips_when_disabled(tmp_path: Path) -> None:
    llm = _RecordingLLM()
    config = LLMConfig(api_key="sk-test", enable_file_extract=False)

    extractor = register_read_document(llm, config, tmp_path / "cache")

    assert llm.registered == {}
    assert extractor is None


@pytest.mark.asyncio
async def test_sweep_deletes_file_extract_files_only(tmp_path: Path) -> None:
    http = _FakeHTTP()
    http.list_payload = {
        "data": [
            {"id": "f1", "purpose": "file-extract"},
            {"id": "f2", "purpose": "image"},
            {"id": "f3", "purpose": "file-extract"},
        ]
    }
    extractor = _extractor(tmp_path, http)

    deleted = await extractor.sweep_remote()

    assert deleted == 2
    assert http.deletes == [
        "https://api.moonshot.cn/v1/files/f1",
        "https://api.moonshot.cn/v1/files/f3",
    ]


@pytest.mark.asyncio
async def test_sweep_tolerates_individual_delete_failures(tmp_path: Path) -> None:
    http = _FakeHTTP()
    http.list_payload = {
        "data": [
            {"id": "f1", "purpose": "file-extract"},
            {"id": "f2", "purpose": "file-extract"},
        ]
    }
    http.fail_delete_ids = {"f1"}
    extractor = _extractor(tmp_path, http)

    deleted = await extractor.sweep_remote()

    assert deleted == 1  # f2 still deleted despite f1 failing


@pytest.mark.asyncio
async def test_sweep_list_failure_returns_zero(tmp_path: Path) -> None:
    http = _FakeHTTP()
    http.fail_get = RuntimeError("network down")
    extractor = _extractor(tmp_path, http)

    deleted = await extractor.sweep_remote()  # must not raise

    assert deleted == 0
    assert http.deletes == []
