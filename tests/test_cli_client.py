"""The deployangel command's API client against a real local HTTP server,
including its failure paths: errors raise, because someone is waiting."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from deployangel.cli.client import ApiError, Client, NotFound, Unauthorized


class Handler(BaseHTTPRequestHandler):
    routes = {}
    requests = []

    def do_GET(self):
        self._answer()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        Handler.requests.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
        self._answer()

    def _answer(self):
        if self.command == "GET":
            Handler.requests.append((self.command, self.path, dict(self.headers), b""))
        status, body = Handler.routes.get(self.path.split("?")[0], (404, b'{"error": "deployment not found"}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def api():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Handler.routes, Handler.requests = {}, []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_reads_json_and_sends_the_token(api):
    Handler.routes["/api/v1/deployments"] = (200, json.dumps({"deployments": [{"id": 42}]}).encode())
    client = Client("da_test_token", endpoint=api)

    assert client.deployments(commit="abc1234", version=None, limit=1) == [{"id": 42}]
    method, path, headers, _ = Handler.requests[-1]
    assert path == "/api/v1/deployments?commit=abc1234&limit=1"
    assert headers["Authorization"] == "Bearer da_test_token"
    assert headers["User-Agent"].startswith("deployangel-cli-python/")


def test_reads_the_setup_document_for_doctor(api):
    Handler.routes["/api/v1/setup"] = (200, b'{"app": {"name": "shop"}, "gaps": []}')

    assert Client("da_live_agent", endpoint=api).setup()["app"] == {"name": "shop"}
    assert Handler.requests[-1][:2] == ("GET", "/api/v1/setup")


def test_posts_checks_as_json(api):
    Handler.routes["/api/v1/deployments/commit%3Aabc1234/checks"] = (201, b'{"id": 1, "deployment_id": 42}')
    result = Client("t", endpoint=api).report_check("commit:abc1234", name="smoke", status="pass", covers=["GET /"])

    assert result["deployment_id"] == 42
    assert json.loads(Handler.requests[-1][3]) == {"name": "smoke", "status": "pass", "covers": ["GET /"]}


def test_raises_not_found_unauthorized_and_other_errors(api):
    client = Client("t", endpoint=api)
    with pytest.raises(NotFound, match="deployment not found"):
        client.verification(99)

    Handler.routes["/api/v1/token"] = (401, b'{"error": "invalid token"}')
    with pytest.raises(Unauthorized, match="invalid token"):
        client.token_info()

    Handler.routes["/api/v1/deployments/latest"] = (502, b"<html>bad gateway</html>")
    with pytest.raises(ApiError, match=r"unexpected response \(HTTP 502\)"):
        client.latest_deployment()


def test_raises_when_the_server_cant_be_reached():
    client = Client("t", endpoint="http://127.0.0.1:9", timeout=2)
    with pytest.raises(ApiError, match="could not reach"):
        client.token_info()


def test_needs_a_token():
    with pytest.raises(Unauthorized, match="DEPLOYANGEL_API_TOKEN is not set"):
        Client(None)
