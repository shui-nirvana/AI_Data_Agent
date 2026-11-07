"""Opt-in Nginx integration test using disposable containers and a mock upstream."""

import http.client
import json
import os
from pathlib import Path
import socket
import ssl
from uuid import uuid4

import pytest

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.skipif(
    os.environ.get("AI_AGENT_RUN_PROXY_TESTS") != "1",
    reason="Nginx integration requires explicit Docker opt-in and local test images",
)

ROOT = Path(__file__).resolve().parents[1]
NGINX_IMAGE = "nginx:1.27-alpine"
PYTHON_IMAGE = "python:3.12-slim"

UPSTREAM = """
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        data = json.dumps({"path": self.path, "body": body.decode(),
                           "token": self.headers.get("X-Demo-Token"),
                           "goal": self.headers.get("X-Task-Goal"),
                           "content_type": self.headers.get("Content-Type"),
                           "forwarded": self.headers.get("X-Forwarded-For"),
                           "real_ip": self.headers.get("X-Real-IP"),
                           "scheme": self.headers.get("X-Forwarded-Proto")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
    do_GET = handle_request
    do_POST = handle_request
ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
"""


def test_nginx_tls_forwarding_limits_and_upstream_failure(tmp_path):
    client = docker.from_env(timeout=30)
    for image in (NGINX_IMAGE, PYTHON_IMAGE):
        client.images.get(image)

    network = client.networks.create(f"agent-proxy-test-{uuid4().hex}")
    containers = []
    try:
        client.containers.run(
            PYTHON_IMAGE,
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", "/test/key.pem", "-out", "/test/cert.pem", "-days", "1",
             "-subj", "/CN=agent.example.com", "-addext", "subjectAltName=DNS:agent.example.com"],
            volumes={str(tmp_path): {"bind": "/test", "mode": "rw"}},
            remove=True,
        )
        upstream_name = f"agent-upstream-{uuid4().hex}"
        upstream = client.containers.run(
            PYTHON_IMAGE, ["python", "-c", UPSTREAM], name=upstream_name,
            network=network.name, detach=True,
        )
        containers.append(upstream)
        config = (ROOT / "deploy" / "nginx.conf.example").read_text(encoding="utf-8")
        config = config.replace("127.0.0.1:8000", f"{upstream_name}:8000")
        config = config.replace("/etc/letsencrypt/live/agent.example.com/fullchain.pem", "/test/cert.pem")
        config = config.replace("/etc/letsencrypt/live/agent.example.com/privkey.pem", "/test/key.pem")
        (tmp_path / "default.conf").write_text(config, encoding="utf-8")
        proxy = client.containers.run(
            NGINX_IMAGE, name=f"agent-nginx-{uuid4().hex}", network=network.name,
            ports={"80/tcp": ("127.0.0.1", None), "443/tcp": ("127.0.0.1", None)},
            volumes={
                str(tmp_path / "default.conf"): {"bind": "/etc/nginx/conf.d/default.conf", "mode": "ro"},
                str(tmp_path): {"bind": "/test", "mode": "ro"},
            },
            detach=True,
        )
        containers.append(proxy)
        result = proxy.exec_run(["nginx", "-t"])
        assert result.exit_code == 0, result.output.decode(errors="replace")
        proxy.reload()
        ports = proxy.attrs["NetworkSettings"]["Ports"]
        http_port = int(ports["80/tcp"][0]["HostPort"])
        https_port = int(ports["443/tcp"][0]["HostPort"])

        plain = http.client.HTTPConnection("127.0.0.1", http_port, timeout=10)
        plain.request("GET", "/health?check=1")
        response = plain.getresponse()
        assert response.status == 308
        assert response.getheader("Location") == "https://agent.example.com/health?check=1"
        response.read()
        plain.close()

        context = ssl.create_default_context(cafile=str(tmp_path / "cert.pem"))
        with socket.create_connection(("127.0.0.1", https_port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname="agent.example.com") as tls:
                assert tls.version() in ("TLSv1.2", "TLSv1.3")

        https = http.client.HTTPSConnection(
            "127.0.0.1", https_port, timeout=10, context=ssl._create_unverified_context()
        )
        https.request("POST", "/api/tasks", body=b"a,b\n1,2\n", headers={
            "Host": "agent.example.com",
            "Content-Type": "text/csv",
            "X-Demo-Token": "test-token",
            "X-Task-Goal": "Preserve%20refunds",
            "X-Forwarded-For": "198.51.100.10",
        })
        response = https.getresponse()
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store"
        forwarded = json.loads(response.read())
        assert forwarded["path"] == "/api/tasks"
        assert forwarded["body"] == "a,b\n1,2\n"
        assert forwarded["token"] == "test-token"
        assert forwarded["goal"] == "Preserve%20refunds"
        assert forwarded["content_type"] == "text/csv"
        assert forwarded["scheme"] == "https"
        assert forwarded["forwarded"] == forwarded["real_ip"] != "198.51.100.10"

        https.putrequest("POST", "/api/tasks", skip_host=True)
        https.putheader("Host", "agent.example.com")
        https.putheader("Content-Length", str(10 * 1024 * 1024 + 1))
        https.endheaders()
        response = https.getresponse()
        assert response.status == 413
        response.read()
        https.close()

        https = http.client.HTTPSConnection(
            "127.0.0.1", https_port, timeout=10, context=ssl._create_unverified_context()
        )
        statuses = []
        for _ in range(50):
            https.request("GET", "/health", headers={"Host": "agent.example.com"})
            response = https.getresponse()
            statuses.append(response.status)
            response.read()
        assert 429 in statuses
        https.close()

        upstream.stop(timeout=2)
        # Use a separate client IP so the earlier rate limit does not mask the upstream error.
        probe = client.containers.run(
            PYTHON_IMAGE,
            ["python", "-c", "import http.client, ssl; "
             f"c = http.client.HTTPSConnection('{proxy.name}', 443, "
             "context=ssl._create_unverified_context(), timeout=10); "
             "c.request('GET', '/', headers={'Host': 'agent.example.com'}); "
             "print(c.getresponse().status)"],
            network=network.name, detach=True,
        )
        containers.append(probe)
        assert probe.wait(timeout=20)["StatusCode"] == 0
        assert probe.logs().strip() == b"502"
    finally:
        for container in reversed(containers):
            container.remove(force=True)
        network.remove()
