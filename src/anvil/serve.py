"""Read-only HTTP access to saved and in-progress run ledgers.

Every route is a GET that reads the ledger through :mod:`anvil.queries` and the
artifact tree through :mod:`anvil.telemetry`. Nothing here writes Git, SQLite, or
an artifact, and nothing starts an agent or a verification command: the
coordinator remains the only writer, so serving a run can never affect it.

The operator starts this server as its own process. It is deliberately not
attached to ``run`` or ``resume``, which must not carry unbounded background work.
"""

from __future__ import annotations

from http import HTTPStatus
import http.server
import ipaddress
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

from .contracts import ContractError
from .queries import (DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, list_runs, run_attempts,
                      run_events, run_summary, run_tasks)
from .store import TERMINAL_RUN_STATUSES, StoreError
from .telemetry import run_telemetry

API_VERSION = 1
"""The read contract's version, independent of the ledger's storage version.

Additive within a version; see docs/CLOUD_SYNC.md. It travels on /health, on
every page envelope, and as an unidentified SSE meta event, so a consumer never
has to infer which contract it is reading.
"""

ASSETS = Path(__file__).parent / "assets"
PAGES = {"index.html": "text/html; charset=utf-8",
         "app.css": "text/css; charset=utf-8",
         "app.js": "text/javascript; charset=utf-8"}
POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; "
          "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
          "form-action 'none'; frame-ancestors 'none'")

RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
POLL_SECONDS = 1.0
KEEPALIVE_SECONDS = 15.0
DEFAULT_MAX_STREAMS = 8


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class _Streams:
    """A hard cap on concurrent event streams, so idle clients cannot accumulate."""

    def __init__(self, limit: int):
        self.limit, self.active = limit, 0
        self.lock = threading.Lock()
        self.stopping = threading.Event()

    def acquire(self) -> bool:
        with self.lock:
            if self.active >= self.limit:
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.active -= 1


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "anvil-serve"
    sys_version = ""

    # -- request plumbing -------------------------------------------------

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _reject_host(self) -> bool:
        """Refuse a request whose Host is not a loopback authority.

        The bind address is already loopback, but a DNS name that resolves to
        127.0.0.1 would otherwise let a page in a browser reach this API under
        an origin the operator never chose.
        """
        header = self.headers.get("Host", "")
        authority = header.rsplit(":", 1)[0] if header.count(":") == 1 else header
        authority = authority.strip("[]")
        if authority and _loopback(authority):
            return False
        self._send(HTTPStatus.FORBIDDEN, {"error": "host is not a loopback authority"})
        return True

    def _send(self, status: HTTPStatus, body: dict) -> None:
        payload = json.dumps({"api_version": API_VERSION, **body}, indent=2).encode() + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # No CORS header: a page from another origin must not read this API.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self._reject_host():
            return
        parsed = urlparse(self.path)
        parameters = parse_qs(parsed.query)
        try:
            self._route(parsed.path.rstrip("/") or "/", parameters)
        except ContractError as exc:
            # Anything the caller can fix by changing the request.
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except StoreError as exc:
            # Parameters are bounded before any query runs, so a store error
            # here means the ledger is missing, uninitialized, or unreadable.
            self._send(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as exc:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    # -- parameters -------------------------------------------------------

    @staticmethod
    def _one(parameters: dict, name: str) -> str | None:
        values = parameters.get(name, [])
        if len(values) > 1:
            raise ContractError(f"{name} may only be given once")
        return values[0] if values else None

    def _limit(self, parameters: dict) -> int:
        """Bound the page here, so no out-of-range limit ever reaches a query."""
        value = self._one(parameters, "limit")
        if value is None:
            return DEFAULT_PAGE_SIZE
        try:
            limit = int(value)
        except ValueError:
            raise ContractError("limit must be an integer") from None
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ContractError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        return limit

    def _after(self, parameters: dict, *, numeric: bool, default=None):
        value = self._one(parameters, "after")
        if value is None:
            return default
        if not numeric:
            return value
        return self._cursor(value, "after")

    @staticmethod
    def _cursor(value: str, name: str) -> int:
        try:
            cursor = int(value)
        except ValueError:
            raise ContractError(f"{name} must be an integer cursor") from None
        if cursor < 0:
            raise ContractError(f"{name} must be a nonnegative cursor")
        return cursor

    def _run_dir(self, run_id: str) -> Path:
        """Resolve a path-supplied run ID inside the state root, or refuse it."""
        if not RUN_ID.match(run_id):
            raise StoreError("unknown run: invalid run ID")
        state_dir = self.server.state_dir
        run_dir = state_dir / run_id
        if run_dir.is_symlink():
            raise StoreError("unknown run: run directory is a symlink")
        resolved = run_dir.resolve()
        if resolved.parent != state_dir.resolve() or not resolved.is_dir():
            raise StoreError("unknown run")
        return resolved

    # -- routes -----------------------------------------------------------

    def _asset(self, name: str) -> None:
        try:
            body = (ASSETS / name).read_bytes()
        except OSError:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "no such asset"})
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", PAGES[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", POLICY)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _route(self, path: str, parameters: dict) -> None:
        if path == "/":
            return self._asset("index.html")
        if path.lstrip("/") in PAGES:
            return self._asset(path.lstrip("/"))
        if path == "/health":
            return self._send(HTTPStatus.OK,
                              {"status": "ok", "state_dir": str(self.server.state_dir)})

        if path == "/api/runs":
            return self._send(HTTPStatus.OK, list_runs(
                self.server.state_dir, after=self._after(parameters, numeric=False),
                limit=self._limit(parameters)))
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "api" or parts[1] != "runs" or len(parts) > 4:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "no such route"})
        run_dir = self._run_dir(parts[2])
        if len(parts) == 3:
            summary = run_summary(run_dir)
            summary["run_dir"] = str(run_dir)
            return self._send(HTTPStatus.OK, summary)
        limit = self._limit(parameters)
        if parts[3] == "tasks":
            return self._send(HTTPStatus.OK, run_tasks(
                run_dir, after=self._after(parameters, numeric=True), limit=limit))
        if parts[3] == "attempts":
            return self._send(HTTPStatus.OK, run_attempts(
                run_dir, after=self._after(parameters, numeric=True), limit=limit))
        if parts[3] == "events":
            return self._send(HTTPStatus.OK, run_events(
                run_dir, after=self._after(parameters, numeric=True, default=0), limit=limit))
        if parts[3] == "telemetry":
            return self._send(HTTPStatus.OK, run_telemetry(
                run_dir, after=self._after(parameters, numeric=True), limit=limit))
        if parts[3] == "stream":
            return self._stream(run_dir, parameters)
        return self._send(HTTPStatus.NOT_FOUND, {"error": "no such route"})

    # -- event stream -----------------------------------------------------

    def _resume_from(self, parameters: dict) -> int:
        """Prefer the browser's own resumption header over the query parameter."""
        header = self.headers.get("Last-Event-ID")
        if header is not None:
            return self._cursor(header, "Last-Event-ID")
        return self._after(parameters, numeric=True, default=0)

    def _stream(self, run_dir: Path, parameters: dict) -> None:
        after = self._resume_from(parameters)
        run_summary(run_dir)  # Refuse an unreadable ledger before opening a stream.
        streams = self.server.streams
        if not streams.acquire():
            return self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                              {"error": "too many concurrent streams"})
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(
                f'event: meta\ndata: {{"api_version": {API_VERSION}}}\n\n'.encode())
            self.wfile.flush()
            self._pump(run_dir, after, streams)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            streams.release()

    def _pump(self, run_dir: Path, after: int, streams: _Streams) -> None:
        """Drain new events, then idle; end once a finished run has nothing left."""
        idle = time.monotonic()
        while not streams.stopping.is_set():
            page = run_events(run_dir, after=after)
            for event in page["items"]:
                self.wfile.write(
                    f"id: {event['id']}\nevent: {event['kind']}\n"
                    f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
                after = event["id"]
            if page["next_after"] is not None:
                continue
            if run_summary(run_dir)["status"] in TERMINAL_RUN_STATUSES:
                # A named event, not a comment: EventSource cannot observe a
                # comment, so a client would reconnect to a finished run forever.
                # It carries no ID, leaving the caller's resumption point intact.
                self.wfile.write(b'event: end\ndata: {"reason": "run complete"}\n\n')
                self.wfile.flush()
                return
            if time.monotonic() - idle >= KEEPALIVE_SECONDS:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                idle = time.monotonic()
            if streams.stopping.wait(POLL_SECONDS):
                return


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state_dir: Path, *, max_streams: int):
        self.state_dir = Path(state_dir)
        self.streams = _Streams(max_streams)
        super().__init__(address, Handler)


def create(state_dir: Path, *, host: str = "127.0.0.1", port: int = 0,
           max_streams: int = DEFAULT_MAX_STREAMS) -> Server:
    """Bind a read-only server, refusing any exposure beyond this machine.

    A ledger carries ticket text, repository paths, review findings, and cost.
    Reaching it from another device is an SSH tunnel; binding it to a network
    needs the session-token design in docs/DELIVERY_DASHBOARD_PLAN.md.
    """
    if not _loopback(host):
        raise ContractError(
            f"refusing to bind {host}: anvil serve is loopback-only; "
            "forward the port over SSH to reach it from another device")
    if not isinstance(max_streams, int) or isinstance(max_streams, bool) or max_streams < 1:
        raise ContractError("max streams must be a positive integer")
    state_dir = Path(state_dir).expanduser()
    if not state_dir.is_dir():
        raise ContractError(f"state directory does not exist: {state_dir}")
    return Server((host, port), state_dir, max_streams=max_streams)


def serve(state_dir: Path, *, host: str = "127.0.0.1", port: int = 0,
          max_streams: int = DEFAULT_MAX_STREAMS, announce=print) -> int:
    server = create(state_dir, host=host, port=port, max_streams=max_streams)
    bound_host, bound_port = server.server_address[:2]
    announce(f"anvil serve reading {server.state_dir}")
    announce(f"http://{bound_host}:{bound_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        announce("stopping; runs and evidence are untouched")
    finally:
        server.streams.stopping.set()
        server.shutdown()
        server.server_close()
    return 0
