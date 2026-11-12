"""Import original arXiv PDFs through the same service as browser uploads."""

import re
import tempfile
import time

import httpx

from .library import LibraryError


ARXIV_ID = re.compile(
    r"(?:[0-9]{4}\.[0-9]{4,5}|[a-z-]+(?:\.[A-Za-z]{2})?/[0-9]{7})(?:v[1-9][0-9]*)?"
)


def import_arxiv_pdf(library, paper_id):
    if not isinstance(paper_id, str) or len(paper_id) > 64 or not ARXIV_ID.fullmatch(paper_id):
        raise ValueError("Provide an arXiv paper ID such as 1706.03762v7 or hep-th/9901001")
    url = f"https://arxiv.org/pdf/{paper_id}"
    deadline = time.monotonic() + 90
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, dir=library.blobs.staging) as source:
        with httpx.stream(
            "GET", url, timeout=15, headers={"Accept": "application/pdf"}
        ) as response:
            response.raise_for_status()
            size = 0
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError("The arXiv PDF download exceeded its time limit")
                size += len(chunk)
                if size > library.MAX_BYTES:
                    raise LibraryError(413, "File exceeds the 25 MiB upload limit")
                source.write(chunk)
        source.seek(0)
        return library.upload(f"arxiv-{paper_id.replace('/', '-')}.pdf", source)
