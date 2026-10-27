import io
import sys
import unittest
import zipfile
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "app.pyw"
SPEC = spec_from_loader("qq_homework_xlsx_test", SourceFileLoader("qq_homework_xlsx_test", str(APP_PATH)))
APP = module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def column_letter(index):
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


class Workbook:
    """拼一个最小的 xlsx：共享字符串、工作表、合并单元格都能造。"""

    def __init__(self):
        self.shared = []
        self.sheets = []

    def add_sheet(self, rows, merges=()):
        xml_rows = []
        for row_index, row in enumerate(rows, start=1):
            cells = []
            for column_index, item in enumerate(row):
                if item is None:
                    continue  # 空格子：Excel 里就是不写这个 c 节点
                kind, value = item
                reference = f"{column_letter(column_index)}{row_index}"
                if kind == "s":
                    if value not in self.shared:
                        self.shared.append(value)
                    cells.append(f'<c r="{reference}" t="s"><v>{self.shared.index(value)}</v></c>')
                elif kind == "n":
                    cells.append(f'<c r="{reference}"><v>{value}</v></c>')
                elif kind == "i":
                    cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>')
                else:
                    raise AssertionError(f"未知单元格类型 {kind}")
            xml_rows.append(f'<row r="{row_index}">' + "".join(cells) + "</row>")
        merge_xml = ""
        if merges:
            merge_xml = (f'<mergeCells count="{len(merges)}">'
                         + "".join(f'<mergeCell ref="{ref}"/>' for ref in merges)
                         + "</mergeCells>")
        self.sheets.append(
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{NS}"><sheetData>' + "".join(xml_rows) + f"</sheetData>{merge_xml}</worksheet>")

    def build(self):
        count = len(self.sheets)
        sheets = "".join(f'<sheet name="S{index}" sheetId="{index}" r:id="rId{index}"/>'
                         for index in range(1, count + 1))
        rels = "".join(
            f'<Relationship Id="rId{index}" Type="{REL_NS}/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, count + 1))
        shared_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      f'<sst xmlns="{NS}" count="{len(self.shared)}" uniqueCount="{len(self.shared)}">'
                      + "".join(f"<si><t>{text}</t></si>" for text in self.shared)
                      + "</sst>")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml",
                             '<?xml version="1.0" encoding="UTF-8"?>'
                             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                             '<Default Extension="xml" ContentType="application/xml"/></Types>')
            archive.writestr("xl/workbook.xml",
                             '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                             f'<workbook xmlns="{NS}" xmlns:r="{REL_NS}"><sheets>{sheets}</sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels",
                             '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                             f'<Relationships xmlns="{PKG_REL_NS}">{rels}</Relationships>')
            archive.writestr("xl/sharedStrings.xml", shared_xml)
            for index, sheet in enumerate(self.sheets, start=1):
                archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet)
        return buffer.getvalue()


def s(text):
    return ("s", text)


def n(value):
    return ("n", value)


class XlsxRosterTest(unittest.TestCase):
    def parse(self, workbook, name="名单.xlsx"):
        return APP.parse_roster_upload(name, workbook.build())["rows"]

    def test_basic_table(self):
        book = Workbook()
        book.add_sheet([
            [s("姓名"), s("学号"), s("QQ号"), s("班级")],
            [s("张三"), n("2024001"), n("12345678"), s("通信2301")],
        ])
        rows = self.parse(book)
        self.assertEqual([(r["name"], r["student_id"], r["qq_number"], r["group_name"]) for r in rows],
                         [("张三", "2024001", "12345678", "通信2301")])

    def test_two_level_merged_header(self):
        book = Workbook()
        book.add_sheet([
            [s("姓名"), s("学号"), s("联系方式"), None],
            [None, None, s("QQ号"), s("邮箱")],
            [s("李四"), n("2024002"), n("22223333"), s("li@qq.com")],
        ], merges=["C1:D1"])
        rows = self.parse(book)
        self.assertEqual((rows[0]["name"], rows[0]["qq_number"], rows[0]["email"]),
                         ("李四", "22223333", "li@qq.com"))

    def test_scientific_notation_numbers(self):
        # Excel 常把长数字存成 1.2345678E7，取出来要还原成整数写法
        book = Workbook()
        book.add_sheet([
            [s("姓名"), s("学号"), s("QQ")],
            [s("王五"), n("2.024003E6"), n("1.1112222E7")],
        ])
        rows = self.parse(book)
        self.assertEqual((rows[0]["student_id"], rows[0]["qq_number"]), ("2024003", "11112222"))

    def test_sparse_columns_keep_alignment(self):
        book = Workbook()
        book.add_sheet([
            [s("姓名"), None, s("学号"), s("QQ号")],
            [s("赵六"), None, n("2024004"), n("33334444")],
        ])
        rows = self.parse(book)
        self.assertEqual((rows[0]["name"], rows[0]["student_id"], rows[0]["qq_number"]),
                         ("赵六", "2024004", "33334444"))

    def test_roster_on_second_sheet(self):
        book = Workbook()
        book.add_sheet([[s("说明")], [s("本表为封面")]])
        book.add_sheet([
            [s("学生姓名"), s("学 号"), s("QQ")],
            [s("孙七"), s("2024005"), n("44445555")],
        ])
        rows = self.parse(book)
        self.assertEqual(rows[0]["qq_number"], "44445555")

    def test_inline_strings(self):
        book = Workbook()
        book.add_sheet([
            [("i", "姓名"), ("i", "学号"), ("i", "QQ号")],
            [("i", "周八"), ("i", "2024006"), ("i", "55556666")],
        ])
        rows = self.parse(book)
        self.assertEqual((rows[0]["name"], rows[0]["qq_number"]), ("周八", "55556666"))

    def test_rich_text_shared_string(self):
        # 富文本共享字符串：一个单元格的文字被拆成多个 run，要拼起来
        book = Workbook()
        book.add_sheet([
            [s("姓名"), s("学号"), s("QQ号")],
            [s("占位"), n("2024008"), n("66667777")],
        ])
        buffer = io.BytesIO(book.build())
        with zipfile.ZipFile(buffer) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        parts["xl/sharedStrings.xml"] = parts["xl/sharedStrings.xml"].replace(
            "<si><t>占位</t></si>".encode("utf-8"),
            "<si><r><t>吴</t></r><r><t>九</t></r></si>".encode("utf-8"))
        rows = APP.parse_roster_upload("名单.xlsx", self.repack(parts))["rows"]
        self.assertEqual((rows[0]["name"], rows[0]["qq_number"]), ("吴九", "66667777"))

    @staticmethod
    def repack(parts):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in parts.items():
                archive.writestr(name, data)
        return buffer.getvalue()

    def test_duplicate_qq_flagged(self):
        book = Workbook()
        book.add_sheet([
            [s("姓名"), s("学号"), s("QQ号")],
            [s("甲"), s("2024009"), n("12345678")],
            [s("乙"), s("2024010"), n("12345678")],
        ])
        rows = self.parse(book)
        self.assertEqual(rows[1]["error"], "文件中存在重复 QQ 号")

    def test_rejects_unknown_extension(self):
        with self.assertRaises(ValueError) as ctx:
            APP.parse_roster_upload("名单.txt", b"abc")
        self.assertIn("Excel", str(ctx.exception))

    def test_legacy_xls_suffix_accepted(self):
        # .xls / .xlsm / .xlsx 都在白名单里；老版 .xls 需要本机 Office 转换
        for suffix in (".xls", ".xlsm", ".xlsx"):
            self.assertIn(suffix, APP.ROSTER_SUFFIXES)

    def test_legacy_xls_goes_through_conversion(self):
        book = Workbook()
        book.add_sheet([
            [s("姓名"), s("学号"), s("QQ号")],
            [s("张三"), n("2024001"), n("12345678")],
        ])
        converted = book.build()
        calls = []
        original = APP.convert_legacy_office

        def fake_convert(kind, content):
            calls.append(kind)
            return converted

        APP.convert_legacy_office = fake_convert
        try:
            rows = APP.parse_roster_upload("名单.xls", b"\xd0\xcf\x11\xe0legacy")["rows"]
        finally:
            APP.convert_legacy_office = original
        self.assertEqual(calls, ["excel"])
        self.assertEqual((rows[0]["name"], rows[0]["qq_number"]), ("张三", "12345678"))

    def test_xlsx_skips_conversion(self):
        book = Workbook()
        book.add_sheet([
            [s("姓名"), s("学号"), s("QQ号")],
            [s("张三"), n("2024001"), n("12345678")],
        ])
        calls = []
        original = APP.convert_legacy_office
        APP.convert_legacy_office = lambda kind, content: calls.append(kind)
        try:
            APP.parse_roster_upload("名单.xlsx", book.build())
        finally:
            APP.convert_legacy_office = original
        self.assertEqual(calls, [])

    def test_broken_xlsx_message(self):
        with self.assertRaises(ValueError) as ctx:
            APP.parse_roster_upload("名单.xlsx", b"not-a-zip")
        self.assertIn(".xlsx", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
