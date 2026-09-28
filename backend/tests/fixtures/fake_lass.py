"""fake LASS 环境校验服务器（T11 测试夹具，系统边界 fake）。

以真实 HTTP 服务器扮演 LASS（环境中台），契约与 app.lass 客户端冻结的
三值协议一致：

    POST /env/check  {"required_topology": {...} | null}
      → {"result": "ready"}
      → {"result": "needs_create", "missing": [...]}
      → {"result": "needs_modify", "diff": "...", "changes": [...]}

测试通过 ``respond()`` 编程下一条响应（默认恒 ready），``requests`` 留痕
全部请求体，用于断言"闸门在 LASS 之前"（如沙盒闸门拦截时不应打到 LASS）。
"""
import http.server
import json
import socketserver
import threading


class FakeLass:
    """可编程三值响应的 fake LASS（线程内 HTTP 服务器）。"""

    def __init__(self):
        self._next: dict = {"result": "ready"}
        self.requests: list[dict] = []
        self._server = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), self._make_handler()
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-lass", daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def respond(self, body: dict) -> None:
        """编程下一次及后续校验的响应体。"""
        self._next = body

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _make_handler(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/env/check":
                    fake.requests.append(body)
                    payload = json.dumps(fake._next).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):  # 静默测试服务器日志
                pass

        return Handler
