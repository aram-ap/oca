"""Exercise the actual HTTP boundary without requiring a GPU or Ollama."""

import asyncio
import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from models.config import ModelConfig
from models.core.llm import OllamaClient, OllamaError
from models.main import check_model


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responses = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append((self.path, json.loads(self.rfile.read(
                    int(self.headers["Content-Length"])).decode())))
                status, body = owner.responses.pop(0)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = OllamaClient(ModelConfig(
            ollama_url=f"http://127.0.0.1:{self.server.server_port}", timeout_s=2))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def reply(self, message):
        self.responses.append((200, json.dumps({"done": True, "message": message})))

    def test_complete_smoke_round_trip(self):
        self.reply({"role": "assistant", "content": "READY"})
        self.reply({"role": "assistant", "content": "", "tool_calls": [{
            "function": {"name": "get_service_health", "arguments": {"service": "orders-api"}}
        }]})
        self.reply({"role": "assistant", "content": "HEALTHY"})
        with redirect_stdout(io.StringIO()):
            asyncio.run(check_model(self.client.config, True))
        self.assertEqual(len(self.requests), 3)
        path, payload = self.requests[-1]
        self.assertEqual(path, "/api/chat")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["model"], "nemotron3:33b")
        self.assertEqual(payload["options"], {"num_predict": 8192})
        self.assertEqual(payload["messages"][-1]["role"], "tool")
        self.assertTrue(json.loads(payload["messages"][-1]["content"])["healthy"])

    def test_http_and_malformed_responses_fail(self):
        for status, body in [(404, '{"error":"missing model"}'), (200, 'invalid'),
                             (200, '{"done":false}'), (200, '{"error":"failed"}')]:
            with self.subTest(status=status, body=body):
                self.responses.append((status, body))
                with self.assertRaises(OllamaError):
                    asyncio.run(self.client.chat([{"role": "user", "content": "READY"}]))

    def test_malformed_tool_arguments_fail(self):
        self.reply({"role": "assistant", "content": "", "tool_calls": [{
            "function": {"name": "bad", "arguments": "not an object"}
        }]})
        with self.assertRaises(OllamaError):
            asyncio.run(self.client.chat([{"role": "user", "content": "check"}]))

    def test_connection_failure(self):
        # Reserve then close a port to produce a refused local connection.
        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = server.server_port
        server.server_close()
        client = OllamaClient(ModelConfig(ollama_url=f"http://127.0.0.1:{port}", timeout_s=1))
        with self.assertRaises(OllamaError):
            asyncio.run(client.chat([{"role": "user", "content": "check"}]))

    def test_configuration_rejects_remote_destinations_and_bad_timeouts(self):
        for url in ["https://localhost", "http://example.com", "http://localhost/path",
                    "http://user:secret@localhost", "http://localhost?host=example.com"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                ModelConfig(ollama_url=url)
        for timeout in [0, -1, float("nan"), float("inf")]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ModelConfig(timeout_s=timeout)
        for tokens in (0, -1, 2.5):
            with self.assertRaises(ValueError):
                ModelConfig(max_output_tokens=tokens)


if __name__ == "__main__":
    unittest.main()
