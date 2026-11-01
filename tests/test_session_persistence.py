"""授权码持久化与自动连接的回归测试。

重点锁住一个真实踩过的坑：`has_saved` 曾经只在后台自动连接线程执行到
load_session() 时才有值，而浏览器 0.7 秒就打开了页面，那一刻接口返回
has_saved=False，界面显示"点击连接 QQ 邮箱"。用户因此以为授权码没保存，
每次重启都去重新生成一个，反而把已经有效的授权码作废掉。

跑法：python -m unittest discover -s tests
"""
from __future__ import annotations

import imaplib
import importlib.machinery
import importlib.util
import json
import socket as _socket
import sys
import tempfile
import unittest
from pathlib import Path

APP_FILE = Path(__file__).resolve().parent.parent / "app.pyw"


def load_app():
    """app.pyw 不是合法模块名，需要显式按文件加载。"""
    if "qq_app_under_test" in sys.modules:
        return sys.modules["qq_app_under_test"]
    loader = importlib.machinery.SourceFileLoader("qq_app_under_test", str(APP_FILE))
    spec = importlib.util.spec_from_loader("qq_app_under_test", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qq_app_under_test"] = module
    loader.exec_module(module)
    return module


class SessionBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig = {
            "SESSION_FILE": self.app.SESSION_FILE,
            "LOG_FILE": self.app.LOG_FILE,
            "LOG_MAX_BYTES": self.app.LOG_MAX_BYTES,
            "SESSION_CACHE": dict(self.app.SESSION_CACHE),
        }
        # 绝不能碰真实 data/session.bin —— 那里面是用户真实的授权码
        self.app.SESSION_FILE = self.tmp / "session.bin"
        self.app.LOG_FILE = self.tmp / "app.log"
        self.app.SESSION_CACHE.update(
            {"saved_account": "", "auto_state": "idle", "auto_error": "", "saved_error": ""})

    def tearDown(self):
        self.app.SESSION_FILE = self._orig["SESSION_FILE"]
        self.app.LOG_FILE = self._orig["LOG_FILE"]
        self.app.LOG_MAX_BYTES = self._orig["LOG_MAX_BYTES"]
        self.app.SESSION_CACHE.update(self._orig["SESSION_CACHE"])
        self._tmp.cleanup()


class SessionRoundTripTests(SessionBase):
    def test_save_then_load_returns_same_credentials(self):
        self.app.save_session("teacher@qq.com", "abcdefghijklmnop")
        self.assertEqual(self.app.load_session(), ("teacher@qq.com", "abcdefghijklmnop"))

    def test_saved_file_is_encrypted_not_plaintext(self):
        self.app.save_session("teacher@qq.com", "abcdefghijklmnop")
        blob = self.app.SESSION_FILE.read_bytes()
        self.assertNotIn(b"teacher@qq.com", blob, "账号不能以明文落盘")
        self.assertNotIn(b"abcdefghijklmnop", blob, "授权码不能以明文落盘")

    def test_clear_session_removes_file_and_cache(self):
        self.app.save_session("teacher@qq.com", "abcdefghijklmnop")
        self.app.clear_session()
        self.assertFalse(self.app.SESSION_FILE.exists())
        self.assertEqual(self.app.load_session(), ("", ""))
        self.assertEqual(self.app.SESSION_CACHE["saved_account"], "")


class PrimeSessionCacheTests(SessionBase):
    """这一组是本次修复的核心回归点。"""

    def test_prime_sets_has_saved_without_network(self):
        """预读只解密不联网，必须让首帧 has_saved 就为 True。"""
        self.app.save_session("teacher@qq.com", "abcdefghijklmnop")
        self.app.SESSION_CACHE.update({"saved_account": "", "auto_state": "idle"})
        self.app.prime_session_cache()
        self.assertTrue(self.app.SESSION_CACHE["saved_account"],
                        "预读后 saved_account 必须有值，否则界面会显示成未连接")
        self.assertEqual(self.app.SESSION_CACHE["auto_state"], "connecting")
        # 没有真正登录：连接态仍为空
        self.assertIsNone(self.app.SESSION["account"])

    def test_prime_without_saved_file_stays_idle(self):
        self.app.prime_session_cache()
        self.assertEqual(self.app.SESSION_CACHE["saved_account"], "")
        self.assertEqual(self.app.SESSION_CACHE["auto_state"], "idle")
        self.assertEqual(self.app.SESSION_CACHE["saved_error"], "")


class BrokenSessionFileTests(SessionBase):
    """密文解不开时不能静默，要留下可排查的原因。"""

    def test_missing_file_is_silent(self):
        self.assertEqual(self.app.load_session(), ("", ""))
        self.assertEqual(self.app.SESSION_CACHE["saved_error"], "")

    def test_garbage_file_reports_decrypt_failure(self):
        self.app.SESSION_FILE.write_bytes(b"not-a-dpapi-blob" * 4)
        self.assertEqual(self.app.load_session(), ("", ""))
        self.assertIn("解密失败", self.app.SESSION_CACHE["saved_error"])

    def test_non_object_payload_reported(self):
        self.app.SESSION_FILE.write_bytes(self.app._dpapi(b"[1,2,3]", True))
        self.assertEqual(self.app.load_session(), ("", ""))
        self.assertIn("格式异常", self.app.SESSION_CACHE["saved_error"])

    def test_missing_auth_code_reported(self):
        payload = json.dumps({"account": "a@qq.com"}).encode("utf-8")
        self.app.SESSION_FILE.write_bytes(self.app._dpapi(payload, True))
        self.assertEqual(self.app.load_session(), ("", ""))
        self.assertIn("缺少", self.app.SESSION_CACHE["saved_error"])

    def test_load_never_raises(self):
        for blob in (b"", b"\x00", b"{}", b"x" * 5000):
            self.app.SESSION_FILE.write_bytes(blob)
            self.app.load_session()  # 不允许抛异常


class LoginErrorClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()

    def test_auth_error_mentions_regenerate(self):
        kind, message = self.app.classify_login_error(imaplib.IMAP4.error("LOGIN failed"))
        self.assertEqual(kind, "auth")
        self.assertIn("授权码", message)

    def test_timeout_is_network_not_auth(self):
        """网络问题绝不能提示"重新生成授权码"，否则用户会白白作废有效授权码。"""
        kind, message = self.app.classify_login_error(_socket.timeout("timed out"))
        self.assertEqual(kind, "network")
        self.assertNotIn("重新生成", message)

    def test_oserror_is_network(self):
        kind, _ = self.app.classify_login_error(OSError("Connection refused"))
        self.assertEqual(kind, "network")

    def test_unknown_error_does_not_crash(self):
        kind, message = self.app.classify_login_error(RuntimeError("boom"))
        self.assertEqual(kind, "unknown")
        self.assertIn("boom", message)


class LogLineTests(SessionBase):
    def test_log_line_appends_and_rotates(self):
        self.app.LOG_MAX_BYTES = 200
        for index in range(30):
            self.app.log_line(f"第 {index} 行日志")
        self.assertTrue(self.app.LOG_FILE.exists())
        self.assertTrue(self.app.LOG_FILE.with_suffix(".log.1").exists(),
                        "超过上限后应轮转出 .1，避免长期运行写满磁盘")

    def test_log_line_never_raises_on_unwritable_path(self):
        blocked = self.tmp / "占位的文件，不是目录"
        blocked.write_text("x", encoding="utf-8")
        self.app.LOG_FILE = blocked / "app.log"
        self.app.log_line("写不进去也不能抛")  # 不抛即通过


if __name__ == "__main__":
    unittest.main()
