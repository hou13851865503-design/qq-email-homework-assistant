import io
import http.client
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


APP_PATH = Path(__file__).resolve().parents[1] / "app.pyw"
SPEC = spec_from_loader("qq_homework_assistant_test", SourceFileLoader("qq_homework_assistant_test", str(APP_PATH)))
APP = module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


def make_docx(rows):
    cells = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>" for value in row) + "</w:tr>"
        for row in rows
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:tbl>{cells}</w:tbl></w:body></w:document>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def make_text_pdf(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    page[NameObject("/Resources")] = resources
    stream = DecodedStreamObject()
    stream.set_data(("BT /F1 12 Tf 50 700 Td (" + text.replace("(", "\\(").replace(")", "\\)") + ") Tj ET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class RosterImportTests(unittest.TestCase):
    def test_docx_table_fields_and_category(self):
        content = make_docx([
            ["姓名", "学号", "QQ号", "班级"],
            ["张三", "20231234", "12345678", "通信2301"],
        ])
        result = APP.parse_roster_upload("名单.docx", content)
        self.assertEqual(result["rows"][0]["name"], "张三")
        self.assertEqual(result["rows"][0]["student_id"], "20231234")
        self.assertEqual(result["rows"][0]["qq_number"], "12345678")
        self.assertEqual(result["rows"][0]["group_name"], "通信2301")
        self.assertEqual(result["rows"][0]["error"], "")

    def test_duplicate_identifiers_are_marked(self):
        content = make_docx([
            ["姓名", "学号", "QQ号"],
            ["张三", "20231234", "12345678"],
            ["李四", "20231235", "12345678"],
        ])
        result = APP.parse_roster_upload("名单.docx", content)
        self.assertIn("重复 QQ 号", result["rows"][1]["error"])

    def test_searchable_pdf_table(self):
        content = make_text_pdf("name student_id qq_number group_name Alice 20231234 12345678 Lab-A")
        result = APP.parse_roster_upload("名单.pdf", content)
        self.assertEqual(result["rows"][0]["name"], "Alice")
        self.assertEqual(result["rows"][0]["group_name"], "Lab-A")

    def test_scanned_pdf_reports_ocr_limitation(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(ValueError, "OCR"):
            APP.parse_roster_upload("扫描.pdf", output.getvalue())

    def test_preview_and_atomic_import_preserve_existing_email(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = APP.DB_PATH
            APP.DB_PATH = Path(temp_dir) / "test.sqlite3"
            APP.initialize()
            server = APP.LocalServer(("127.0.0.1", 0), APP.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                content = make_docx([
                    ["姓名", "学号", "QQ号", "班级"],
                    ["张三", "20231234", "12345678", "通信2301"],
                ])
                connection = http.client.HTTPConnection("127.0.0.1", port)
                connection.request("POST", "/api/roster/import-preview", content, {
                    "Content-Type": "application/octet-stream",
                    "X-Upload-Filename": "list.docx",
                })
                preview_response = connection.getresponse()
                preview = json.loads(preview_response.read())
                self.assertEqual(preview_response.status, 200)
                self.assertEqual(preview["rows"][0]["group_name"], "通信2301")

                def import_rows(rows):
                    payload = json.dumps({"action": "import-parsed", "rows": rows}).encode("utf-8")
                    request = http.client.HTTPConnection("127.0.0.1", port)
                    request.request("POST", "/api/roster", payload, {"Content-Type": "application/json"})
                    response = request.getresponse()
                    return response.status, json.loads(response.read())

                status, _ = import_rows([{
                    "name": "张三", "student_id": "20231234", "qq_number": "12345678", "group_name": "通信2301",
                }])
                self.assertEqual(status, 200)
                with APP.db() as conn:
                    conn.execute("UPDATE roster SET email=?,email_verified=1 WHERE qq_number=?", ("student@example.com", "12345678"))

                status, result = import_rows([{
                    "name": "张三更新", "student_id": "20231234", "qq_number": "12345678", "group_name": "通信2302",
                }])
                self.assertEqual(status, 200)
                self.assertEqual(result["updated"], 1)
                with APP.db() as conn:
                    member = conn.execute("SELECT * FROM roster WHERE qq_number=?", ("12345678",)).fetchone()
                    self.assertEqual(member["email"], "student@example.com")
                    self.assertEqual(member["email_verified"], 1)

                status, _ = import_rows([
                    {"name": "新成员", "student_id": "20239999", "qq_number": "12345679", "group_name": "通信2301"},
                    {"name": "冲突成员", "student_id": "20231234", "qq_number": "12345680", "group_name": "通信2301"},
                ])
                self.assertEqual(status, 400)
                with APP.db() as conn:
                    self.assertIsNone(conn.execute("SELECT 1 FROM roster WHERE qq_number=?", ("12345679",)).fetchone())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                APP.DB_PATH = original_db_path


if __name__ == "__main__":
    unittest.main()
