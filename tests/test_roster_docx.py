import io
import sys
import unittest
import zipfile
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "app.pyw"
SPEC = spec_from_loader("qq_homework_docx_test", SourceFileLoader("qq_homework_docx_test", str(APP_PATH)))
APP = module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def paragraph(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def cell(text, span=1):
    span_xml = f'<w:gridSpan w:val="{span}"/>' if span > 1 else ""
    return (f"<w:tc><w:tcPr>{span_xml}</w:tcPr>"
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>")


def table(rows):
    return "<w:tbl>" + "".join("<w:tr>" + "".join(row) + "</w:tr>" for row in rows) + "</w:tbl>"


def build_docx(body):
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{NS}"><w:body>{body}</w:body></w:document>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>")
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


class DocxRosterTest(unittest.TestCase):
    def parse(self, body):
        return APP.parse_roster_upload("名单.docx", build_docx(body))["rows"]

    def test_table_with_title_paragraph(self):
        rows = self.parse(paragraph("通信2301班 学生名单") + table([
            [cell("姓名"), cell("学号"), cell("QQ号"), cell("班级")],
            [cell("张三"), cell("2024001"), cell("12345678"), cell("通信2301")],
        ]))
        self.assertEqual([(r["name"], r["student_id"], r["qq_number"], r["group_name"]) for r in rows],
                         [("张三", "2024001", "12345678", "通信2301")])

    def test_two_level_merged_header(self):
        rows = self.parse(table([
            [cell("姓名"), cell("学号"), cell("联系方式", span=2)],
            [cell(""), cell(""), cell("QQ号"), cell("邮箱")],
            [cell("张三"), cell("2024001"), cell("12345678"), cell("a@qq.com")],
        ]))
        self.assertEqual(rows[0]["name"], "张三")
        self.assertEqual(rows[0]["qq_number"], "12345678")
        self.assertEqual(rows[0]["email"], "a@qq.com")

    def test_header_aliases(self):
        rows = self.parse(table([
            [cell("学生姓名"), cell("学 号"), cell("QQ"), cell("小组")],
            [cell("王五"), cell("2024003"), cell("11112222"), cell("第三组")],
        ]))
        self.assertEqual((rows[0]["name"], rows[0]["group_name"]), ("王五", "第三组"))

    def test_plain_paragraph_rows(self):
        rows = self.parse(paragraph("姓名 学号 QQ号") + paragraph("赵六 2024004 33334444"))
        self.assertEqual(rows[0]["qq_number"], "33334444")

    def test_rejects_unsupported_extension(self):
        with self.assertRaises(ValueError):
            APP.parse_roster_upload("名单.txt", b"abc")

    def test_accepts_doc_extension(self):
        # .doc 走 Word 转换；这里只验证扩展名不再被"仅支持 .docx"拒绝
        try:
            APP.parse_roster_upload("名单.doc", b"not-a-real-doc")
        except ValueError as exc:
            self.assertNotIn("仅支持 .docx", str(exc))


if __name__ == "__main__":
    unittest.main()
