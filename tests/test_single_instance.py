"""单实例检测与版本替换的回归测试。

背景：`find_port()` 以前只校验"应用名 + 目录"，不校验代码版本。于是改完
代码重启时，新进程发现 8765 上有个同名应用，就直接打开它的页面并退出——
用户以为重启了，其实一直在跑旧代码，反馈"改了还是原样"。

另一个坑：探测用 `urllib.request.urlopen`，会被系统代理（FlClash 等）劫持，
127.0.0.1 的请求也走代理，探测必然失败，于是每次启动都新开一个端口。
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import threading
import socket
import time
import unittest
import urllib.request
from pathlib import Path

APP_FILE = Path(__file__).resolve().parent.parent / "app.pyw"


def load_app():
    if "qq_app_single_instance" in sys.modules:
        return sys.modules["qq_app_single_instance"]
    loader = importlib.machinery.SourceFileLoader("qq_app_single_instance", str(APP_FILE))
    spec = importlib.util.spec_from_loader("qq_app_single_instance", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qq_app_single_instance"] = module
    loader.exec_module(module)
    return module


class BuildIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()

    def test_build_is_stable_and_identifiable(self):
        self.assertTrue(self.app.BUILD)
        self.assertNotEqual(self.app.BUILD, "unknown", "正常目录下应能算出 build")
        # 同一份文件多次计算必须一致，否则同版本也会被判成"旧版本"而互相替换
        self.assertEqual(self.app.BUILD, self.app._build_id())

    def test_health_includes_build(self):
        """健康检查必须带 build，否则新进程无法识别端口上是哪个版本。"""
        app = self.app
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = app.LocalServer(("127.0.0.1", port), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data.get("build"), app.BUILD)
            self.assertEqual(data.get("name"), "QQ作业助手")
        finally:
            server.shutdown()
            server.server_close()


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()

    def test_probe_dead_port_returns_none(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # 关掉后端口无人监听
        self.assertIsNone(self.app._probe_instance(port, timeout=0.3))

    def test_probe_recognizes_own_instance(self):
        """本应用的实例必须能被识别出来，否则单实例复用形同虚设。"""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = self.app.LocalServer(("127.0.0.1", port), self.app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            info = self.app._probe_instance(port, timeout=2)
            self.assertIsNotNone(info)
            self.assertEqual(info.get("build"), self.app.BUILD)
        finally:
            server.shutdown()
            server.server_close()

    def test_shutdown_request_on_dead_port_is_false(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.assertFalse(self.app._ask_old_instance_to_quit(port))


class ShutdownEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()

    def test_shutdown_accepts_loopback_and_stops_server(self):
        """收到退出请求后 serve_forever() 必须返回，main() 才能结束、进程才退出。

        注意不要断言"端口立刻可 bind"：shutdown() 只让服务循环停下，
        监听 socket 要到进程退出/close 才释放，在同一进程内测会误判。
        """
        app = self.app
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = app.LocalServer(("127.0.0.1", port), app.Handler)
        stopped = threading.Event()

        def serve():
            server.serve_forever()
            stopped.set()

        threading.Thread(target=serve, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/shutdown", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST")
            with opener.open(request, timeout=5) as response:
                self.assertTrue(json.loads(response.read().decode("utf-8")).get("ok"))
            self.assertTrue(stopped.wait(3), "收到退出请求后 serve_forever() 应返回")
        finally:
            try:
                server.server_close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
