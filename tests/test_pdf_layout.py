import io
import os

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from agent_harness import HarnessConfig
from agent_harness.infrastructure.document_parser import DocumentParser
from agent_harness.interfaces.web.app import create_app
from test_library import FILES, HEADERS
from test_services import FakeClient, wait_until


def layout_fixture():
    writer = PdfWriter()
    for number in (1, 2):
        page = writer.add_blank_page(width=400, height=500)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {
                        NameObject("/F1"): DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject("/Helvetica"),
                            }
                        )
                    }
                ),
            }
        )
        lines = [
            (30, 475, "REPEATED running header"),
            (30, 25, f"Page {number} of 2"),
            (30, 400, f"Useful body {number}"),
        ]
        graphics = ""
        if number == 1:
            lines += [
                (35, 340, "Planet"),
                (155, 340, "Mass"),
                (35, 310, "Saturn"),
                (155, 310, "95"),
            ]
            graphics = "30 300 240 60 re S\n150 300 m 150 360 l S\n30 330 m 270 330 l S\n"
        stream = DecodedStreamObject()
        stream.set_data(
            (
                graphics
                + "\n".join(f"BT /F1 12 Tf {x} {y} Td ({label}) Tj ET" for x, y, label in lines)
            ).encode()
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    sink = io.BytesIO()
    writer.write(sink)
    return sink.getvalue()


def test_long_tables_preserve_headers_and_row_ranges():
    parser = DocumentParser()
    rows = [["Planet", "Measure"], *[[f"Planet-{i}", "value " * 50] for i in range(20)]]
    sections = parser._layout_sections(
        [
            {
                "page": 1,
                "lines": [],
                "tables": [
                    {"bbox": [10, 20, 200, 300], "rows": rows},
                ],
            }
        ]
    )
    table = sections[-1]
    chunks = list(parser._table_chunks(table))
    assert len(chunks) > 1
    assert all(
        len(chunk["text"]) <= 1200 and chunk["text"].startswith("Planet | Measure\n")
        for chunk in chunks
    )
    assert chunks[0]["location"]["start_row"] == 2
    assert chunks[-1]["location"]["end_row"] == len(rows)
    for i in range(20):
        assert sum(f"Planet-{i} |" in chunk["text"] for chunk in chunks) == 1


def test_live_pdf_layout_table_reaches_rag(tmp_path):
    endpoint = os.getenv("HARNESS_PDF_LAYOUT_TEST_URL")
    if not endpoint:
        pytest.skip("HARNESS_PDF_LAYOUT_TEST_URL is unset")
    config = HarnessConfig(tmp_path, "fake", pdf_layout_url=endpoint)
    app = create_app(config, FakeClient)
    with TestClient(app) as client:
        response = client.post(
            FILES, headers=HEADERS, files={"file": ("layout.pdf", layout_fixture())}
        )
        assert response.status_code == 201
        document = response.json()
        wait_until(
            lambda: app.state.ingestion.status(document["id"])["status"] == "ready", timeout=20
        )
        sections = app.state.ingestion.structure(document["id"])
        assert sections
        result = client.post(
            "/api/agent/library/search",
            headers=HEADERS,
            json={"query": "Saturn", "mode": "keyword"},
        )
        assert result.status_code == 200
        hits = result.json()["hits"]
        assert len(hits) == 1
        hit = hits[0]
        assert hit["content_type"] == "table" and hit["location"]["page"] == 1
        assert hit["text"] == "Planet | Mass\nSaturn | 95"
        assert not app.state.ingestion.search("REPEATED")["hits"]
        assert "Page 2 of 2" not in str(sections)
        assert "Useful body 2" in str(sections)
