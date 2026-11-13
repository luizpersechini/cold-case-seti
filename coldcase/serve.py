"""Local dashboard server: http://localhost:8790

Serves the dashboard (rebuilt on every load) and gives its two buttons
something to talk to: start a runner batch now, or stop the one in progress.
Listens on this machine only. The buttons work only from the dashboard page
itself: requests must come from this address and carry a header that other
web pages cannot send.

usage: python -m coldcase.serve
"""

import importlib
import os
import re
import shutil
import signal
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import dashboard, liveview
from .remote import ROOT

HOST, PORT = "127.0.0.1", 8790
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
LOCK = ROOT / "cases" / "runner.lock"
PLOT = re.compile(r"/cases/evidence/([A-Za-z0-9_]+)/(waterfall(?:_blind)?\.png)")


def runner_pid():
    """Process id of the run in progress, or None."""
    if LOCK.exists():
        pid = LOCK.read_text().strip()
        if pid.isdigit():
            try:
                os.kill(int(pid), 0)
                return int(pid)
            except OSError:
                return None
    return None


def start_run():
    if runner_pid():
        return "A run is already in progress."
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    log = open(ROOT / "cases" / "runner.launchd.log", "a")
    subprocess.Popen(
        [
            uv,
            "run",
            "--project",
            str(ROOT),
            "python",
            "-I",
            "-m",
            "coldcase.runner",
            "run",
            "--now",
        ],
        cwd=str(ROOT),
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return "Started. It stops at the limits in runner.toml."


def stop_run():
    pid = runner_pid()
    if not pid:
        return "No run is in progress."
    try:
        os.killpg(os.getpgid(pid), signal.SIGINT)
    except OSError:
        os.kill(pid, signal.SIGINT)
    return "Stopping. Cases already settled are kept."


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _local(self):
        """Only this page may call the buttons: right Host, same Origin if
        given, and a header a cross-site form or image cannot add."""
        origin = self.headers.get("Origin")
        return (
            self.headers.get("Host", "") in ALLOWED_HOSTS
            and (origin is None or origin in {f"http://{h}" for h in ALLOWED_HOSTS})
            and self.headers.get("X-ColdCase") == "1"
        )

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return self._send(403, "forbidden")
        if path in ("/", "/index.html"):
            return self._send(302, "", extra={"Location": "/report/dashboard.html"})
        if path == "/report/dashboard.html":
            try:
                # pick up changes to the page code without restarting the server
                importlib.reload(liveview)
                importlib.reload(dashboard)
                out = dashboard.build()
            except Exception as e:
                return self._send(500, f"dashboard failed: {type(e).__name__}: {e}")
            return self._send(200, out.read_bytes(), "text/html; charset=utf-8")
        m = PLOT.fullmatch(path)
        if m:
            f = ROOT / "cases" / "evidence" / m[1] / m[2]
            if f.exists():
                return self._send(200, f.read_bytes(), "image/png")
        self._send(404, "not found")

    def do_POST(self):  # noqa: N802
        if not self._local():
            return self._send(403, "forbidden")
        path = self.path.split("?")[0]
        if path == "/run":
            return self._send(200, start_run())
        if path == "/stop":
            return self._send(200, stop_run())
        self._send(404, "not found")

    def log_message(self, *args):  # keep the log quiet
        pass


def main():
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if e.errno == 48:  # address already in use
            print(f"A dashboard server is already running at http://localhost:{PORT}. "
                  "Use that one, or stop it first (Ctrl-C in its terminal tab).")
            return
        raise
    print(f"dashboard at http://localhost:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
