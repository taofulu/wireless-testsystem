"""wts-worker 真实进程守护：只在命令行外部接缝断言行为。

用标准库 HTTP 服务器扮演后端，不依赖后端进程，也不直接调用内部函数。
"""
import http.server
import socketserver
import subprocess
import sys
import threading
from pathlib import Path


def _worker_exe() -> str:
    # console script 安装在当前解释器同目录（venv/bin 或系统 bin）
    exe = Path(sys.executable).parent / "wts-worker"
    assert exe.exists(), f"wts-worker 未随包安装: {exe}"
    return str(exe)


class _OKHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默测试服务器日志
        pass


def _serve_ok() -> int:
    server = socketserver.TCPServer(("127.0.0.1", 0), _OKHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def test_help_lists_check_command():
    proc = subprocess.run([_worker_exe(), "--help"], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0
    assert "check" in proc.stdout


def test_check_ok_against_live_server():
    port = _serve_ok()
    proc = subprocess.run(
        [_worker_exe(), "check", "--server", f"http://127.0.0.1:{port}"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_check_unreachable_exit_code():
    proc = subprocess.run(
        [_worker_exe(), "check", "--server", "http://127.0.0.1:1"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 1
    assert proc.stderr
