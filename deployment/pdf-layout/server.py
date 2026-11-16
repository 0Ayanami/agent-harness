"""Local PDF layout extraction. The application decides how elements are indexed."""

import argparse
import io
import json
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer

import pdfplumber
from pdfminer.pdfexceptions import PDFException


def extract(data):
    pages = []
    with pdfplumber.open(io.BytesIO(data), unicode_norm="NFC") as pdf:
        if len(pdf.pages) > 500:
            raise ValueError("PDF exceeds the 500 page extraction limit")
        total = 0
        for page in pdf.pages:
            tables = []
            for table in sorted(
                page.find_tables(), key=lambda table: (table.bbox[1], table.bbox[0])
            ):
                rows = table.extract()
                if len(rows) > 1 and len(rows[0]) > 1 and any(cell for row in rows for cell in row):
                    tables.append({"bbox": table.bbox, "rows": rows})

            def outside_tables(obj):
                if obj["object_type"] != "char":
                    return True
                x = (obj["x0"] + obj["x1"]) / 2
                y = (obj["top"] + obj["bottom"]) / 2
                return not any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in boxes)

            boxes = [table["bbox"] for table in tables]
            lines = []
            for line in page.filter(outside_tables).extract_text_lines(return_chars=False):
                margin = (
                    "header"
                    if line["bottom"] <= page.height * 0.10
                    else "footer"
                    if line["top"] >= page.height * 0.90
                    else None
                )
                lines.append({"text": line["text"], "margin": margin})
            total += sum(len(line["text"]) for line in lines)
            total += sum(
                len(cell or "") + 1 for table in tables for row in table["rows"] for cell in row
            )
            if total > 2_000_000:
                raise ValueError("PDF text exceeds the extraction limit")
            pages.append({"page": page.page_number, "lines": lines, "tables": tables})
            page.close()
    return pages


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(200 if self.path == "/health" else 404, {})

    def do_POST(self):
        if self.path != "/extract":
            self.reply(404, {})
            return
        size = self.headers.get("Content-Length", "")
        if not (
            0 < len(size) <= 8
            and size.isascii()
            and size.isdecimal()
            and 0 < int(size) <= 25 * 1024 * 1024
        ):
            self.reply(413, {})
            return
        if self.headers.get("Content-Type") != "application/pdf":
            self.reply(415, {})
            return
        self.connection.settimeout(15)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                pages = extract(self.rfile.read(int(size)))
        except (PDFException, ValueError, OSError):
            self.reply(422, {})
            return
        self.reply(200, {"pages": pages})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    HTTPServer((args.host, 8079), Handler).serve_forever()
