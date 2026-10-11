import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from scripts.request_abuse import local_target, request


@pytest.mark.parametrize("url", ["https://localhost", "http://example.com", "http://localhost@evil.test", "http://localhost:bad"])
def test_reject_nonlocal_targets(url):
    with pytest.raises(argparse.ArgumentTypeError):
        local_target(url)


@pytest.mark.parametrize("url", ["http://192.168.1.50:8080/api", "http://10.0.0.5:8080/api", "http://[fd00::1]:8080/api"])
def test_private_lan_targets(url):
    assert local_target(url).geturl() == url


@pytest.mark.parametrize("url", ["http://8.8.8.8", "http://169.254.169.254", "http://0.0.0.0", "http://172.32.0.1"])
def test_reject_addresses_outside_demo_networks(url):
    with pytest.raises(argparse.ArgumentTypeError):
        local_target(url)


def test_redirect_is_recorded_without_following():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.com/")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        result = request(local_target(f"http://127.0.0.1:{server.server_port}"), "/products/1", 1)
        assert result["status"] == 302
    finally:
        server.shutdown()
        worker.join()
        server.server_close()
