"""Bounded document extraction, source locations, and explicit character-based chunks."""

import io
import json
import re
import threading
import unicodedata
import zlib
from collections import Counter
from pathlib import Path

import jieba
import httpx
from pypdf import PdfReader


class DocumentParseError(ValueError):
    def __init__(self, message, *, needs_ocr=False):
        super().__init__(message)
        self.needs_ocr = needs_ocr


class ParsedChunks(list):
    """List-compatible parser result with its explicit section tree attached."""

    def __init__(self, chunks, sections):
        super().__init__(chunks)
        self.sections = sections


class KeywordTokenizer:
    """A private tokenizer whose optional dictionary cache stays in managed artifacts."""

    def __init__(self, cache_dir):
        cache = Path(cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        self.tokenizer = jieba.Tokenizer()
        self.tokenizer.tmp_dir = str(cache)
        self.tokenizer.cache_file = "library-jieba.cache"
        self.lock = threading.Lock()

    def tokens(self, text, *, query=False):
        # Characters that have meaning in MATCH never become query syntax.
        with self.lock:
            words = list(self.tokenizer.cut_for_search(self.normalize(text), HMM=False))
        result = []
        for word in words:
            result.extend(re.findall(r"[\w]+", word, flags=re.UNICODE))
        if query:
            return list(dict.fromkeys(result))[:40]
        return result

    def encode(self, text):
        return " ".join(self.tokens(text))

    @staticmethod
    def normalize(text):
        return unicodedata.normalize("NFKC", text).casefold()


class DocumentParser:
    MAX_CHARACTERS = 2_000_000
    MAX_PAGES = 500
    MAX_PAGE_STREAM = 8 * 1024 * 1024
    CHUNK_CHARACTERS = 1200
    OVERLAP_CHARACTERS = 150

    def __init__(self, *, pdf_layout_url=None):
        self.pdf_layout_url = pdf_layout_url

    def parse(self, path, extension):
        document = self.parse_document(path, extension)
        return ParsedChunks(document["chunks"], document["sections"])

    def parse_document(self, path, extension):
        """Extract a bounded native-text document and its hierarchy.

        Section keys are local to this parse. Persistence assigns version-scoped
        IDs to pages, tables, and text sections.
        """
        if extension == "pdf" and self.pdf_layout_url:
            sections = self._layout_pdf(path)
        elif extension == "pdf":
            sections = [self._section("root", None, 0, "document", None, "", {})]
            for text, location in self._pdf(path):
                sections.append(
                    self._section(
                        f"page-{location['page']}",
                        "root",
                        1,
                        "page",
                        f"Page {location['page']}",
                        text,
                        location,
                        metadata={"extraction_method": "pypdf_native_text"},
                    )
                )
        else:
            sections = self._text(path, markdown=extension == "md")
        chunks = []
        for ordinal, section in enumerate(sections):
            section["ordinal"] = ordinal
            table = section["kind"] == "table"
            parts = (
                self._table_chunks(section)
                if table
                else self._chunks(section["text"], section["location"])
            )
            for chunk in parts:
                chunks.append(
                    {
                        **chunk,
                        "section_key": section["key"],
                        "content_type": "table" if table else "text",
                        "metadata": {"extraction_method": section["metadata"]["extraction_method"]},
                    }
                )
        if not chunks:
            raise DocumentParseError("No readable text was found in this document")
        return {"sections": sections, "chunks": chunks}

    @staticmethod
    def _section(key, parent_key, level, kind, title, text, location, *, metadata=None):
        return {
            "key": key,
            "parent_key": parent_key,
            "level": level,
            "kind": kind,
            "title": title,
            "text": text,
            "location": location,
            "metadata": metadata or {"extraction_method": "native_text"},
        }

    @staticmethod
    def _margin_key(line):
        text = " ".join(unicodedata.normalize("NFKC", line["text"]).casefold().split())
        if re.fullmatch(r"(?:page\s+\d+(?:\s+of\s+\d+)?|第\s*\d+\s*页|\d+\s*/\s*\d+|\d+)", text):
            text = "<page number>"
        return line["margin"], text

    def _layout_pdf(self, path):
        try:
            with Path(path).open("rb") as pdf, httpx.Client(trust_env=False, timeout=90) as client:
                with client.stream(
                    "POST",
                    self.pdf_layout_url.rstrip("/") + "/extract",
                    content=pdf,
                    headers={
                        "Content-Type": "application/pdf",
                        "Content-Length": str(Path(path).stat().st_size),
                    },
                ) as response:
                    if response.status_code == 422:
                        raise DocumentParseError(
                            "PDF layout extraction failed or exceeded its limits"
                        )
                    response.raise_for_status()
                    data = bytearray()
                    for block in response.iter_bytes():
                        data.extend(block)
                        if len(data) > 16 * 1024 * 1024:
                            raise DocumentParseError(
                                "PDF layout response exceeds the extraction limit"
                            )
            pages = json.loads(data)["pages"]
            return self._layout_sections(pages)
        except (httpx.HTTPError, OSError) as error:
            raise DocumentParseError(
                "PDF layout service unavailable; start its Docker container and retry"
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, DocumentParseError):
                raise
            raise DocumentParseError("PDF layout service returned invalid data") from error

    def _layout_sections(self, pages):
        # Count distinct pages, so repeated text on a single page is kept.
        counts = Counter(
            key
            for page in pages
            for key in {self._margin_key(line) for line in page["lines"] if line["margin"]}
        )
        threshold = max(2, (len(pages) + 1) // 2)
        sections = [self._section("root", None, 0, "document", None, "", {})]
        extraction = {"extraction_method": "pdfplumber_layout"}
        for page in pages:
            number = page["page"]
            text = "\n".join(
                line["text"].replace("\x00", "").strip()
                for line in page["lines"]
                if not line["margin"] or counts[self._margin_key(line)] < threshold
            ).strip()
            if not text and not page["tables"]:
                continue
            key, location = f"page-{number}", {"page": number, "start_line": 1}
            sections.append(
                self._section(
                    key, "root", 1, "page", f"Page {number}", text, location, metadata=extraction
                )
            )
            for index, table in enumerate(page["tables"], 1):
                rows = [
                    [" ".join((cell or "").replace("\x00", "").split()) for cell in row]
                    for row in table["rows"]
                ]
                sections.append(
                    self._section(
                        f"{key}-table-{index}",
                        key,
                        2,
                        "table",
                        f"Table {index}",
                        "\n".join(" | ".join(row) for row in rows),
                        {"page": number, "table": index, "bbox": table["bbox"]},
                        metadata={**extraction, "rows": rows},
                    )
                )
        if len(sections) == 1:
            raise DocumentParseError("PDF has no extractable text; OCR is required", needs_ocr=True)
        return sections

    def _table_chunks(self, section):
        rows = section["text"].splitlines()
        header = rows[0]
        prefix = header + "\n" if len(header) <= self.CHUNK_CHARACTERS // 2 else ""
        data = rows[1:] if prefix else rows
        capacity = self.CHUNK_CHARACTERS - len(prefix)
        batch, size, first, last = [], 0, None, 0
        for row_index, row in enumerate(data, 2 if prefix else 1):
            for offset in range(0, max(1, len(row)), capacity):
                part = row[offset : offset + capacity]
                if batch and size + 1 + len(part) > capacity:
                    yield {
                        "text": prefix + "\n".join(batch),
                        "location": {
                            **section["location"],
                            "start_row": first,
                            "end_row": last,
                        },
                    }
                    batch, size, first = [], 0, None
                first = row_index if first is None else first
                last = row_index
                size += (1 if batch else 0) + len(part)
                batch.append(part)
        if batch:
            yield {
                "text": prefix + "\n".join(batch),
                "location": {
                    **section["location"],
                    "start_row": first,
                    "end_row": last,
                },
            }

    def _text(self, path, *, markdown):
        try:
            with Path(path).open("rb") as original:
                prefix = original.read(4)
                original.seek(0)
                encoding = (
                    "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                )
                with io.TextIOWrapper(original, encoding=encoding) as stream:
                    text = stream.read(self.MAX_CHARACTERS + 1)
        except UnicodeError as error:
            raise DocumentParseError("Text indexing requires UTF-8 or BOM-marked UTF-16") from error
        if len(text) > self.MAX_CHARACTERS:
            raise DocumentParseError("Document text exceeds the 2,000,000 character limit")
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        root = self._section("root", None, 0, "document", None, text, {"start_line": 1})
        if not markdown:
            root["location"]["end_line"] = max(1, len(text.splitlines()))
            return [root]
        sections, stack, lines, current = [root], [root], [], root
        fence = None

        def finish():
            current["text"] = "".join(lines)
            current["location"]["end_line"] = current["location"]["start_line"] + max(
                0, len(current["text"].splitlines()) - 1
            )

        for number, line in enumerate(text.splitlines(keepends=True), 1):
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\n"))
            if fence is not None:
                lines.append(line)
                if (
                    marker
                    and marker[1][0] == fence[0]
                    and len(marker[1]) >= fence[1]
                    and not marker[2].strip()
                ):
                    fence = None
                continue
            if marker and (marker[1][0] == "~" or "`" not in marker[2]):
                fence = (marker[1][0], len(marker[1]))
                lines.append(line)
                continue
            match = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|$)", line.rstrip("\n"))
            if match:
                finish()
                level = len(match[1])
                heading = re.sub(r"[ \t]+#+[ \t]*$", "", match[2] or "").strip()
                while stack[-1]["level"] >= level:
                    stack.pop()
                parent = stack[-1]
                ancestors = [item["title"] for item in stack if item["title"]]
                current = self._section(
                    f"section-{len(sections)}",
                    parent["key"],
                    level,
                    "section",
                    heading,
                    "",
                    {
                        "start_line": number,
                        "heading": heading,
                        "heading_path": [*ancestors, heading],
                    },
                    metadata={"extraction_method": "markdown_native_text"},
                )
                sections.append(current)
                stack.append(current)
                lines = []
            lines.append(line)
        finish()
        return sections

    def _check_page_streams(self, page):
        contents = page.get("/Contents")
        if contents is None:
            return
        contents = contents.get_object()
        streams = contents if isinstance(contents, list) else [contents]
        if len(streams) > 256:
            raise DocumentParseError("PDF page contains too many content streams")
        total = 0
        for reference in streams:
            stream = reference.get_object()
            # pypdf retains encoded bytes here. This preflight protects the common
            # Flate case before extract_text constructs its full content stream.
            raw = getattr(stream, "_data", b"")
            if len(raw) > self.MAX_PAGE_STREAM:
                raise DocumentParseError("PDF page content stream is too large")
            filters = stream.get("/Filter")
            if filters == "/FlateDecode" or filters == ["/FlateDecode"]:
                try:
                    decoded = zlib.decompressobj().decompress(raw, self.MAX_PAGE_STREAM + 1)
                except zlib.error as error:
                    raise DocumentParseError(
                        "PDF contains an invalid compressed content stream"
                    ) from error
                total += len(decoded)
            else:
                total += len(raw)
            if total > self.MAX_PAGE_STREAM:
                raise DocumentParseError("PDF page content exceeds the extraction limit")

    def _pdf(self, path):
        try:
            reader = PdfReader(path, strict=False)
            if reader.is_encrypted:
                raise DocumentParseError("Password-protected PDFs cannot be indexed")
            if len(reader.pages) > self.MAX_PAGES:
                raise DocumentParseError("PDF exceeds the 500 page extraction limit")
            sections, total = [], 0
            for number, page in enumerate(reader.pages, 1):
                self._check_page_streams(page)
                text = (page.extract_text() or "").replace("\x00", "")
                total += len(text)
                if total > self.MAX_CHARACTERS:
                    raise DocumentParseError("PDF text exceeds the 2,000,000 character limit")
                if text.strip():
                    sections.append((text, {"page": number, "start_line": 1}))
            if not sections:
                raise DocumentParseError(
                    "PDF has no extractable text; OCR is required", needs_ocr=True
                )
            return sections
        except DocumentParseError:
            raise
        except Exception as error:
            raise DocumentParseError(
                "PDF could not be parsed; check that the file is valid"
            ) from error

    def _chunks(self, text, location):
        start = 0
        while start < len(text):
            end = min(start + self.CHUNK_CHARACTERS, len(text))
            if end < len(text):
                boundary = text.rfind("\n\n", start + self.CHUNK_CHARACTERS // 2, end)
                if boundary < 0:
                    boundary = text.rfind("\n", start + self.CHUNK_CHARACTERS // 2, end)
                if boundary >= 0:
                    end = boundary + 1
            segment = text[start:end]
            content = segment.strip()
            if content:
                leading = len(segment) - len(segment.lstrip())
                first_line = location.get("start_line", 1) + text.count("\n", 0, start + leading)
                located = {key: value for key, value in location.items() if value is not None}
                located["start_line"] = first_line
                located["end_line"] = first_line + content.count("\n")
                yield {"text": content, "location": located}
            if end == len(text):
                break
            start = max(start + 1, end - self.OVERLAP_CHARACTERS)
