"""Check bearer tokens, argument transport, and non-retried HTTP errors."""

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from models.tools.client import ToolAPIClient, ToolAPIError


class ToolAPITests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond(None)

            def do_POST(self):
                self.respond(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def respond(self, body):
                owner.requests.append((self.command, self.path,
                                       self.headers.get("Authorization"), body))
                self.send_response(owner.status)
                if owner.status == 302:
                    self.send_header("Location", "/outside-tools")
                self.end_headers()
                self.wfile.write(b'{"evidence_ref":"sandbox:1"}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = ToolAPIClient(role="engineer", token="engineer-token",
                                   base_url=f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_auth_query_and_json_body(self):
        asyncio.run(self.client.request("GET", "/api/tools/code", {"path": "app/main.py"}))
        result = asyncio.run(self.client.request("POST", "/api/tools/sandbox/patch",
                                                 {"ticket_id": "INC-1", "diff": "patch"}))
        self.assertEqual(self.requests[0][1], "/api/tools/code?path=app%2Fmain.py")
        self.assertEqual(self.requests[1][2], "Bearer engineer-token")
        self.assertEqual(self.requests[1][3], {"ticket_id": "INC-1", "diff": "patch"})
        self.assertEqual(result["evidence_ref"], "sandbox:1")

    def test_reject_redirect_and_server_error_without_retry(self):
        for status in (302, 500):
            self.status = status
            previous = len(self.requests)
            with self.assertRaises(ToolAPIError):
                asyncio.run(self.client.request("POST", "/api/tools/sandbox/patch", {}))
            self.assertEqual(len(self.requests), previous + 1)


if __name__ == "__main__":
    unittest.main()
