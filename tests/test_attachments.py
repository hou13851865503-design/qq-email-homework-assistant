import sys
import unittest
from email.message import EmailMessage
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "app.pyw"
SPEC = spec_from_loader("qq_homework_assistant_attach_test",
                        SourceFileLoader("qq_homework_assistant_attach_test", str(APP_PATH)))
APP = module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


class AttachmentPartsTest(unittest.TestCase):
    def _message(self):
        msg = EmailMessage()
        msg["Subject"] = "第三次作业"
        msg["From"] = "a@qq.com"
        msg["To"] = "b@qq.com"
        msg.set_content("正文")
        msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf",
                           filename="homework.pdf")
        msg.add_attachment(b"docx-bytes", maintype="application",
                           subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                           filename="report.docx")
        msg.add_attachment(b"PK-zip-bytes", maintype="application", subtype="zip",
                           filename="bundle.zip")
        msg.add_attachment(b"rar-bytes", maintype="application",
                           subtype="x-rar-compressed", filename="photos.rar")
        return msg

    def test_collects_every_attachment_type(self):
        names = [p["filename"] for p in APP.attachment_parts(self._message())]
        self.assertEqual(names, ["homework.pdf", "report.docx", "bundle.zip", "photos.rar"])

    def test_extension_is_never_rewritten(self):
        names = [p["filename"] for p in APP.attachment_parts(self._message())]
        self.assertEqual(sum(1 for n in names if n.lower().endswith(".pdf")), 1)
        self.assertTrue(all(p["size"] > 0 for p in APP.attachment_parts(self._message())))

    def test_ordinals_match_part_order(self):
        parts = APP.attachment_parts(self._message())
        self.assertEqual([p["ordinal"] for p in parts], [0, 1, 2, 3])

    def test_attachment_without_filename_is_not_renamed_to_pdf(self):
        msg = EmailMessage()
        msg["Subject"] = "无文件名附件"
        msg.set_content("正文")
        msg.add_attachment(b"raw-bytes", maintype="application", subtype="octet-stream")
        parts = APP.attachment_parts(msg)
        self.assertEqual(len(parts), 1)
        self.assertFalse(parts[0]["filename"].lower().endswith(".pdf"))
        self.assertTrue(parts[0]["filename"].startswith("附件"))


if __name__ == "__main__":
    unittest.main()
