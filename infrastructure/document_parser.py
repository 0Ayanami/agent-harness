"""Bounded document extraction, source locations, and explicit character-based chunks."""

import io
import re
import threading
import unicodedata
import zlib
from pathlib import Path

import jieba
from pypdf import PdfReader


class DocumentParseError(ValueError):
    def __init__(self, message, *, needs_ocr=False):
        super().__init__(message)
        self.needs_ocr = needs_ocr


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

    def parse(self, path, extension):
        if extension == "pdf":
            sections = self._pdf(path)
        else:
            sections = self._text(path, markdown=extension == "md")
        chunks = []
        for text, location in sections:
            chunks.extend(self._chunks(text, location))
        if not chunks:
            raise DocumentParseError("No readable text was found in this document")
        return chunks

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
        if not markdown:
            return [(text, {"start_line": 1})]
        sections, lines, first, heading = [], [], 1, None
        for number, line in enumerate(text.splitlines(keepends=True), 1):
            match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
            if match:
                if lines:
                    sections.append(("".join(lines), {"start_line": first, "heading": heading}))
                lines, first, heading = [], number, match.group(1).strip()
            lines.append(line)
        if lines:
            sections.append(("".join(lines), {"start_line": first, "heading": heading}))
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
