"""Scoped Docker API proxy.

api and worker need to start and stop llama.cpp containers. Giving them the
Docker socket itself would hand them root on the host, so only this service
mounts it, and it forwards just the calls the orchestrator makes, limited to
meshpilot-dyn-* containers created from one image with one fixed volume and
network. Everything else gets a 403.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SOCKET_PATH = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
ALLOWED_IMAGE = os.environ.get("ALLOWED_IMAGE", "meshpilot-llamacpp")
ALLOWED_BIND = os.environ.get("ALLOWED_BIND", "meshpilot_model-storage:/models:ro")
ALLOWED_NETWORK = os.environ.get("ALLOWED_NETWORK", "meshpilot_default")
STATS_ONLY_CONTAINERS = {"meshpilot-pytorch-baseline"}

MAX_BODY_BYTES = 64 * 1024
MAX_MEMORY_BYTES = 64 * 1024 ** 3

DYN_NAME = r"meshpilot-dyn-[A-Za-z0-9][A-Za-z0-9_.-]{0,48}"
DYN_NAME_RE = re.compile(rf"^{DYN_NAME}$")
NAME_FILTER_RE = re.compile(rf"^\^/{DYN_NAME}\$$")
CPUSET_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")
ENV_RES = [
    re.compile(p) for p in (
        r"^LLAMA_MODEL_DIR=/models$",
        r"^LLAMA_HOST=0\.0\.0\.0$",
        r"^LLAMA_PORT=8080$",
        r"^LLAMA_THREADS=\d{1,3}$",
        r"^LLAMA_CTX_SIZE=\d{1,7}$",
        r"^LLAMA_BATCH_SIZE=\d{1,6}$",
        r"^LLAMA_PARALLEL=\d{1,2}$",
        r"^LLAMA_MODEL_FILE=[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$",
    )
]


class Denied(Exception):
    pass


def validate_create_payload(payload: object) -> dict:
    """Returns the payload to forward. Unknown keys are refused rather than
    dropped, so nothing like Privileged or CapAdd can reach the Docker API."""
    if not isinstance(payload, dict) or set(payload) - {"Image", "Env", "HostConfig"}:
        raise Denied("unexpected container fields")
    if payload.get("Image") != ALLOWED_IMAGE:
        raise Denied("image not allowed")

    env = payload.get("Env")
    if not isinstance(env, list) or not all(
        isinstance(e, str) and any(r.match(e) for r in ENV_RES) for e in env
    ):
        raise Denied("environment not allowed")

    host = payload.get("HostConfig")
    if not isinstance(host, dict) or set(host) - {"CpusetCpus", "Memory", "Binds", "NetworkMode", "RestartPolicy"}:
        raise Denied("unexpected host config fields")
    if host.get("Binds") != [ALLOWED_BIND]:
        raise Denied("volume mount not allowed")
    if host.get("NetworkMode") != ALLOWED_NETWORK:
        raise Denied("network not allowed")
    if host.get("RestartPolicy") != {"Name": "no"}:
        raise Denied("restart policy not allowed")
    if not isinstance(host.get("CpusetCpus"), str) or not CPUSET_RE.match(host["CpusetCpus"]):
        raise Denied("cpuset not allowed")
    memory = host.get("Memory")
    if type(memory) is not int or not 0 < memory <= MAX_MEMORY_BYTES:
        raise Denied("memory limit not allowed")
    return payload


def authorize(method: str, target: str) -> None:
    """Raises Denied unless this method and path is one the orchestrator uses."""
    parts = urlsplit(target)
    path, query = parts.path, parse_qs(parts.query, keep_blank_values=True)

    if method == "GET" and path == "/containers/json":
        if set(query) - {"all", "filters"} or "filters" not in query:
            raise Denied("a name filter is required")
        try:
            filters = json.loads(query["filters"][0])
        except ValueError:
            raise Denied("invalid filter")
        names = filters.get("name") if isinstance(filters, dict) else None
        if set(filters or {}) != {"name"} or not names or not all(
            isinstance(n, str) and NAME_FILTER_RE.match(n) for n in names
        ):
            raise Denied("filter must name a meshpilot-dyn container")
        return

    if method == "POST" and path == "/containers/create":
        if set(query) != {"name"} or not DYN_NAME_RE.match(query["name"][0]):
            raise Denied("container name not allowed")
        return

    match = re.match(r"^/containers/([^/]+)(?:/([a-z]+))?$", path)
    if not match:
        raise Denied("path not allowed")
    name, action = match.groups()
    is_dyn = bool(DYN_NAME_RE.match(name))

    if method == "GET" and action == "stats":
        if query != {"stream": ["false"]} or not (is_dyn or name in STATS_ONLY_CONTAINERS):
            raise Denied("stats not allowed")
        return
    if not is_dyn:
        raise Denied("container not allowed")
    if method == "POST" and action == "start" and not query:
        return
    if method == "POST" and action == "stop" and set(query) <= {"t"}:
        return
    if method == "DELETE" and action is None and set(query) <= {"force", "v"}:
        return
    raise Denied("operation not allowed")


class _UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET_PATH)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, reason: str) -> None:
        print(f"DENIED {self.command} {self.path}: {reason}", flush=True)
        self._reply(403, json.dumps({"message": f"docker-proxy: {reason}"}).encode())

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            return self._deny("request body too large")
        body = self.rfile.read(length) if length else b""

        if self.command == "GET" and self.path == "/_health":
            return self._reply(200, b'{"status":"ok"}')

        try:
            authorize(self.command, self.path)
            if self.path.startswith("/containers/create"):
                try:
                    payload = json.loads(body)
                except ValueError:
                    raise Denied("invalid JSON")
                body = json.dumps(validate_create_payload(payload)).encode()
        except Denied as e:
            return self._deny(str(e))

        conn = _UnixConnection("localhost", timeout=60)
        try:
            headers = {"Content-Type": "application/json"} if body else {}
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            self._reply(resp.status, resp.read(), resp.getheader("Content-Type") or "application/json")
        except OSError as e:
            self._reply(502, json.dumps({"message": f"docker-proxy: {e}"}).encode())
        finally:
            conn.close()

    do_GET = do_POST = do_DELETE = _handle

    def _refuse(self) -> None:
        self._deny("method not allowed")

    do_PUT = do_PATCH = do_HEAD = do_OPTIONS = _refuse

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 2375), Handler).serve_forever()
