"""HTTP side of the web interface: static files, a small JSON API and Server-Sent Events."""

from __future__ import annotations

import ipaddress
import json
import shutil
import tempfile
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import quote, urlsplit

from wantabag.config import (
    AUDIO_NOTE,
    AUDIO_QUALITIES,
    RECOMMENDED_AUDIO,
    RECOMMENDED_VIDEO,
    VIDEO_LABELS,
    VIDEO_QUALITIES,
    ConfigError,
    load_config,
)
from wantabag.download import check_ffmpeg
from wantabag.web import jobs
from wantabag.web.jobs import JOB_ERRORS, JobManager, QueueFull

MAX_BODY = 16 * 1024
KEEPALIVE_S = 15
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https:; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_static() -> dict[str, tuple[bytes, str]]:
    """Every packaged static file by URL path. Serving from this dict cannot leave the folder."""
    found: dict[str, tuple[bytes, str]] = {}

    def walk(node, prefix: str) -> None:
        for child in node.iterdir():
            if child.is_dir():
                walk(child, f"{prefix}{child.name}/")
            elif ctype := CONTENT_TYPES.get(Path(child.name).suffix):
                found[prefix + child.name] = (child.read_bytes(), ctype)

    walk(resources.files("wantabag.web") / "static", "/")
    found["/"] = found["/index.html"]
    return found


def content_disposition(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def options_payload() -> dict:
    cfg = load_config()
    return {
        "audio": [
            {"key": k, "label": label, "recommended": k == RECOMMENDED_AUDIO}
            for k, (_, label) in AUDIO_QUALITIES.items()
        ],
        "video": [
            {"key": k, "label": VIDEO_LABELS[k], "recommended": k == RECOMMENDED_VIDEO}
            for k in VIDEO_QUALITIES
        ],
        "audio_note": AUDIO_NOTE.replace("\n", " "),
        "defaults": {
            "quality": cfg.audio_quality,
            "videos": cfg.video_quality if cfg.video_enabled else False,
        },
    }


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    manager: JobManager
    static: dict[str, tuple[bytes, str]]
    allowed_hosts: set[str] | None


class Handler(BaseHTTPRequestHandler):
    server: WebServer
    server_version = "wantabag"

    def log_message(self, format: str, *args) -> None:
        if self.server.manager.verbose:
            super().log_message(format, *args)

    def end_headers(self) -> None:
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        super().end_headers()

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _json_body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise HttpError(415, "Send the body as application/json.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "Invalid Content-Length.") from None
        if length > MAX_BODY:
            raise HttpError(413, "Request body too large.")
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            raise HttpError(400, "The body is not valid JSON.") from None
        if not isinstance(body, dict):
            raise HttpError(400, "The body must be a JSON object.")
        return body

    def do_GET(self) -> None:
        self._route()

    do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_GET

    def _route(self) -> None:
        try:
            host = self.headers.get("Host", "").lower()
            if self.server.allowed_hosts is not None and host not in self.server.allowed_hosts:
                raise HttpError(403, "Unexpected Host header.")
            parts = urlsplit(self.path).path.strip("/").split("/")
            if parts[0] != "api":
                self._static(urlsplit(self.path).path)
            elif parts[1:] == ["options"]:
                self._only("GET")
                self._json(200, options_payload())
            elif parts[1:] == ["preview"]:
                self._only("POST")
                self._preview()
            elif parts[1:] == ["jobs"]:
                self._only("POST")
                self._create_job()
            elif parts[1:2] == ["jobs"] and len(parts) in (3, 4):
                self._only("GET")
                self._job(parts[2], parts[3] if len(parts) == 4 else "")
            else:
                raise HttpError(404, "Not found.")
        except HttpError as e:
            self._json(e.status, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _only(self, method: str) -> None:
        if self.command != method:
            raise HttpError(405, f"Use {method} here.")

    def _static(self, path: str) -> None:
        if self.command not in ("GET", "HEAD"):
            raise HttpError(405, "Use GET here.")
        asset = self.server.static.get(path)
        if asset is None:
            raise HttpError(404, "Not found.")
        self._send(200, asset[0], asset[1], {"Cache-Control": "no-cache"})

    def _preview(self) -> None:
        body = self._json_body()
        try:
            url = jobs.validate_url(body.get("url"))
        except ValueError as e:
            raise HttpError(400, str(e)) from None
        try:
            self._json(200, jobs.preview(url))
        except JOB_ERRORS as e:
            raise HttpError(502, f"Could not read that playlist: {e}") from None

    def _create_job(self) -> None:
        body = self._json_body()
        if hint := check_ffmpeg():
            raise HttpError(503, hint)
        try:
            job = self.server.manager.create(body.get("url"), body)
        except QueueFull as e:
            raise HttpError(429, str(e)) from None
        except ConfigError as e:
            raise HttpError(500, str(e)) from None
        except ValueError as e:
            raise HttpError(400, str(e)) from None
        self._json(
            202,
            {
                "id": job.id,
                "status": job.status,
                "events_url": f"/api/jobs/{job.id}/events",
                "zip_url": f"/api/jobs/{job.id}/zip",
            },
        )

    def _job(self, id: str, what: str) -> None:
        job = self.server.manager.get(id)
        if job is None:
            raise HttpError(404, "This download does not exist or has expired.")
        if what == "":
            self._json(200, job.snapshot())
        elif what == "events":
            self._events(job)
        elif what == "zip":
            self._zip(job)
        else:
            raise HttpError(404, "Not found.")

    def _events(self, job: jobs.Job) -> None:
        try:
            nxt = int(self.headers.get("Last-Event-ID", "-1")) + 1
        except ValueError:
            nxt = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.write(b"retry: 2000\n\n")
        self.wfile.flush()
        while True:
            with job.cond:
                notified = nxt < len(job.events) or job.cond.wait(KEEPALIVE_S)
                batch = job.events[nxt:]
            frames = b"".join(
                f"id: {e['id']}\nevent: {e['type']}\n"
                f"data: {json.dumps(e['data'], ensure_ascii=False)}\n\n".encode()
                for e in batch
            )
            nxt += len(batch)
            if frames or not notified:
                self.wfile.write(frames or b": ping\n\n")
                self.wfile.flush()
            if batch and batch[-1]["type"] == "end":
                return

    def _zip(self, job: jobs.Job) -> None:
        with job.cond:
            path = job.zip_path if job.status == "done" else None
        if path is None:
            raise HttpError(409, "The zip is not ready yet.")
        try:
            fh = open(path, "rb")  # noqa: SIM115
        except FileNotFoundError:
            raise HttpError(404, "This download does not exist or has expired.") from None
        with fh:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", content_disposition(path.name))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            shutil.copyfileobj(fh, self.wfile)
        self.server.manager.remove(job.id)


def make_server(host: str, port: int, manager: JobManager) -> WebServer:
    server = WebServer((host, port), Handler)
    server.manager = manager
    server.static = load_static()
    bound = server.server_address[1]
    server.allowed_hosts = (
        {f"127.0.0.1:{bound}", f"localhost:{bound}"} if is_loopback(host) else None
    )
    return server


def serve(host: str, port: int, open_browser: bool, verbose: bool) -> int:
    manager = JobManager(Path(tempfile.mkdtemp(prefix="wantabag-web-")), verbose=verbose)
    try:
        server = make_server(host, port, manager)
        url = f"http://{host}:{server.server_address[1]}"
        print(f"wantabag web: {url}  (Ctrl+C to stop)", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()
        finally:
            server.server_close()
    finally:
        manager.close()
    return 0
