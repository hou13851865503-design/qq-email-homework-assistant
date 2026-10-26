import http.client
import json
import sys
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch


APP_PATH = Path(__file__).resolve().parents[1] / "app.pyw"
SPEC = spec_from_loader("qq_homework_assistant_announcement_test", SourceFileLoader("qq_homework_assistant_announcement_test", str(APP_PATH)))
APP = module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


class FakeSMTP:
    messages = []
    fail_recipient = ""

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def login(self, account, auth_code):
        self.account = account

    def send_message(self, message):
        self.messages.append(message)
        if message["To"] == self.fail_recipient:
            raise RuntimeError("temporary SMTP failure")
        return {}


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = APP.DB_PATH
        self.original_session = dict(APP.SESSION)
        APP.DB_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        APP.initialize()
        APP.SESSION.update(account="class@example.com", auth_code="test-code")
        self.server = APP.LocalServer(("127.0.0.1", 0), APP.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        FakeSMTP.messages = []
        FakeSMTP.fail_recipient = ""
        with APP.db() as conn:
            conn.executemany("""
                INSERT INTO roster(qq_number,name,student_id,group_name,email,email_verified,active)
                VALUES(?,?,?,?,?,?,?)
            """, [
                ("12345678", "张三", "20231234", "通信2301", "zhangsan@example.com", 1, 1),
                ("12345679", "李四", "20231235", "通信2301", "lisi@example.com", 1, 1),
                ("12345680", "王五", "20231236", "通信2301", "wangwu@example.com", 0, 1),
                ("12345681", "赵六", "20231237", "通信2302", "zhaoliu@example.com", 1, 1),
            ])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        APP.DB_PATH = self.original_db_path
        APP.SESSION.update(self.original_session)
        self.temp_dir.cleanup()

    def post(self, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        body = json.dumps(payload).encode("utf-8")
        connection.request("POST", "/api/announcements", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_health_reports_application_directory(self):
        status, result = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(result["name"], "QQ作业助手")
        self.assertEqual(result["app_dir"], str(APP.APP_DIR))

    def test_send_to_verified_members_individually_and_save_history(self):
        with patch.object(APP.smtplib, "SMTP_SSL", FakeSMTP):
            status, result = self.post({
                "subject": "本周通知",
                "body": "请查看课程安排。",
                "group_filter": "通信2301",
                "qq_numbers": ["12345678", "12345679"],
            })
        self.assertEqual(status, 200)
        self.assertEqual(len(result["sent"]), 2)
        self.assertEqual(len(FakeSMTP.messages), 2)
        self.assertEqual([message["To"] for message in FakeSMTP.messages], ["zhangsan@example.com", "lisi@example.com"])
        self.assertTrue(all(message["Subject"] == "本周通知" for message in FakeSMTP.messages))
        with APP.db() as conn:
            campaign = conn.execute("SELECT * FROM announcements WHERE id=?", (result["announcement_id"],)).fetchone()
            recipients = conn.execute("SELECT status FROM announcement_recipients WHERE announcement_id=? ORDER BY id", (campaign["id"],)).fetchall()
        self.assertEqual(campaign["body"], "请查看课程安排。")
        self.assertEqual([row["status"] for row in recipients], ["sent", "sent"])
        status, history = self.get("/api/announcements/" + str(result["announcement_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(len(history["recipients"]), 2)
        status, app_state = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(app_state["announcements"][0]["sent_count"], 2)

    def test_unverified_member_rejected_before_sending(self):
        with patch.object(APP.smtplib, "SMTP_SSL", FakeSMTP):
            status, result = self.post({
                "subject": "本周通知",
                "body": "请查看课程安排。",
                "group_filter": "通信2301",
                "qq_numbers": ["12345678", "12345680"],
            })
        self.assertEqual(status, 400)
        self.assertIn("没有已核实邮箱", result["error"])
        self.assertEqual(FakeSMTP.messages, [])
        with APP.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 0)

    def test_one_recipient_failure_does_not_stop_remaining_messages(self):
        FakeSMTP.fail_recipient = "lisi@example.com"
        with patch.object(APP.smtplib, "SMTP_SSL", FakeSMTP):
            status, result = self.post({
                "subject": "本周通知",
                "body": "请查看课程安排。",
                "group_filter": "",
                "qq_numbers": ["12345678", "12345679", "12345681"],
            })
        self.assertEqual(status, 200)
        self.assertEqual(len(result["sent"]), 2)
        self.assertEqual(result["errors"][0]["qq_number"], "12345679")
        with APP.db() as conn:
            states = {row["qq_number"]: row["status"] for row in conn.execute(
                "SELECT qq_number,status FROM announcement_recipients WHERE announcement_id=?",
                (result["announcement_id"],),
            )}
        self.assertEqual(states, {"12345678": "sent", "12345679": "failed", "12345681": "sent"})


if __name__ == "__main__":
    unittest.main()
