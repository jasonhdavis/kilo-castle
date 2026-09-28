#!/usr/bin/env python3
"""Shared studio browser management — one headed Chromium with a dedicated
persistent profile that M'Lord logs into once.

The Court Artist attaches to it over CDP (chrome-devtools MCP via
``--browserUrl``) and sees the same logged-in pages. A court-owned injector
(``_inject_worker``) pins the annotation overlay (``court/assets/annotator.js``)
into every page target so M'Lord can pin notes (selector + note) that the
artist reads from the studio worktree's JSONL file.

Stdlib only: subprocess, socket, urllib, base64/sha1 WebSocket client.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants (override-friendly for tests)
# ---------------------------------------------------------------------------

DEFAULT_PORT = 9335
KILO_CASTLE_DIR = Path.home() / ".local" / "share" / "kilo-castle"
STATE_PATH = Path(os.environ.get("COURT_BROWSER_STATE", KILO_CASTLE_DIR / "browser.json"))
PROFILE_PATH = Path(os.environ.get("COURT_BROWSER_PROFILE", KILO_CASTLE_DIR / "studio-chrome-profile"))
LOG_PATH = Path(os.environ.get("COURT_BROWSER_LOG", KILO_CASTLE_DIR / "studio-browser.log"))
STOP_FILE_PATH = Path(os.environ.get("COURT_BROWSER_STOP_FILE", KILO_CASTLE_DIR / "browser-injector.stop"))

CONSOLE_PORT_DEFAULT = 8300
POLL_SECONDS = 2.0
STARTUP_TIMEOUT_SECONDS = 15.0

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ANNOTATOR_JS_PATH = _ASSETS_DIR / "annotator.js"


def find_chrome_binary() -> Optional[str]:
    """Locate a Chromium-family binary; first match wins."""
    for cand in CHROME_CANDIDATES:
        if Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    for name in ("chromium", "google-chrome", "google-chrome-stable"):
        which = shutil.which(name)
        if which:
            return which
    return None


def _free_port(start: int = DEFAULT_PORT) -> int:
    """Return the first bindable port at or above ``start``."""
    port = start
    for _ in range(1000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                port += 1
                continue
        return port
    raise RuntimeError(f"no free port found at or above {start}")


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def _http_get_json(url: str, timeout: float = 3.0) -> Optional[Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def cdp_responsive(port: int) -> bool:
    """True when /json/version answers on this port."""
    return _http_get_json(f"http://127.0.0.1:{int(port)}/json/version", timeout=2.0) is not None


def _load_state() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _remove_state() -> None:
    try:
        STATE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def load_annotator_source(endpoint: str, worktree: str) -> str:
    """Read annotator.js and substitute its two placeholders."""
    try:
        source = ANNOTATOR_JS_PATH.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"annotator script not readable at {ANNOTATOR_JS_PATH}: {e}") from e
    return source.replace("{{ANNOTATE_ENDPOINT}}", endpoint).replace("{{WORKTREE}}", worktree)


def _spawn_detached(cmd: list[str], cwd: Optional[Path] = None) -> int:
    """Spawn a long-running helper that survives this CLI process."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_PATH, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    return proc.pid


# ---------------------------------------------------------------------------
# start / status / stop
# ---------------------------------------------------------------------------


def start_browser(
    headless: bool = False,
    port: Optional[int] = None,
    profile: Optional[Path] = None,
    annotate_worktree: Optional[Path] = None,
) -> dict[str, Any]:
    """Start the shared studio browser (idempotent) and return its info dict.

    If a previously-recorded browser is still alive and CDP-responsive, adopt
    it instead of spawning a second instance. All override parameters accept
    test values.
    """
    existing = _load_state()
    if existing and _pid_alive(existing.get("pid")) and cdp_responsive(int(existing.get("port") or 0)):
        if annotate_worktree and not existing.get("annotator_pid"):
            repo_root = Path(__file__).resolve().parent.parent
            try:
                existing["annotator_pid"] = _spawn_detached(
                    [sys.executable, "-m", "court.cli", "browser", "_inject_worker", str(annotate_worktree)],
                    cwd=repo_root,
                )
                _write_state(existing)
            except Exception:
                pass
        return dict(existing)

    chrome = find_chrome_binary()
    if not chrome:
        raise RuntimeError(
            "no Chromium-family browser found (looked for Google Chrome, Chromium, "
            "Chrome Canary, Microsoft Edge, and `chromium`/`google-chrome` on PATH)"
        )

    profile_dir = Path(profile) if profile else PROFILE_PATH
    profile_dir.mkdir(parents=True, exist_ok=True)
    use_port = int(port) if port else _free_port(DEFAULT_PORT)

    cmd = [
        chrome,
        f"--remote-debugging-port={use_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        cmd.append("--headless=new")
    cmd.append("about:blank")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_PATH, "a", encoding="utf-8")
    try:
        log.write(f"\n--- studio browser start {datetime.now().isoformat()} port {use_port} headless={headless} ---\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()

    deadline = time.time() + STARTUP_TIMEOUT_SECONDS
    while time.time() < deadline:
        if cdp_responsive(use_port):
            break
        if proc.poll() is not None:
            raise RuntimeError(
                f"browser exited during startup (code {proc.returncode}); see {LOG_PATH}"
            )
        time.sleep(0.25)
    else:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        raise RuntimeError(f"CDP did not answer on port {use_port} within {STARTUP_TIMEOUT_SECONDS:.0f}s; see {LOG_PATH}")

    state: dict[str, Any] = {
        "pid": proc.pid,
        "port": use_port,
        "cdp_url": f"http://127.0.0.1:{use_port}",
        "profile": str(profile_dir),
        "headless": bool(headless),
        "started_ts": datetime.now().isoformat(),
        "annotator_pid": None,
    }

    if annotate_worktree:
        repo_root = Path(__file__).resolve().parent.parent
        try:
            state["annotator_pid"] = _spawn_detached(
                [sys.executable, "-m", "court.cli", "browser", "_inject_worker", str(annotate_worktree)],
                cwd=repo_root,
            )
        except Exception as e:
            print(f"warning: annotation injector did not start ({e})", file=sys.stderr)

    _write_state(state)
    return state


def browser_status() -> dict[str, Any]:
    """State file contents plus live liveness and CDP probe results."""
    state = _load_state()
    out: dict[str, Any] = {"state_file": str(STATE_PATH), "recorded": state, "running": False}
    if not state:
        return out
    pid = state.get("pid")
    port = state.get("port")
    alive = _pid_alive(pid)
    version = _http_get_json(f"http://127.0.0.1:{int(port)}/json/version", timeout=2.0) if alive and port else None
    out["pid_alive"] = alive
    out["cdp_responsive"] = version is not None
    out["running"] = bool(alive and version is not None)
    out["cdp"] = {
        "browser": (version or {}).get("Browser"),
        "protocol": (version or {}).get("Protocol-Version"),
        "user_agent": (version or {}).get("User-Agent"),
    }
    annotator_pid = state.get("annotator_pid")
    out["annotator_alive"] = _pid_alive(annotator_pid) if annotator_pid else False
    return out


def _terminate_pid(pid: int, grace_seconds: float = 3.0) -> str:
    """SIGTERM, wait up to grace_seconds, then SIGKILL. Never touches other pids."""
    if not _pid_alive(pid):
        return "already gone"
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not _pid_alive(pid):
            return "terminated"
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    return "killed" if not _pid_alive(pid) else "refused to die"


def stop_browser() -> dict[str, Any]:
    """Stop the browser and injector recorded in OUR state file only."""
    state = _load_state()
    result: dict[str, Any] = {"had_state": state is not None, "actions": []}
    if not state:
        return result
    for key in ("annotator_pid", "pid"):
        pid = state.get(key)
        if pid and _pid_alive(int(pid)):
            result["actions"].append({key: int(pid), "outcome": _terminate_pid(int(pid))})
    _remove_state()
    try:
        STOP_FILE_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Minimal RFC 6455 WebSocket client (client frames MUST be masked)
# ---------------------------------------------------------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketClosed(RuntimeError):
    pass


class WebSocketClient:
    """Tiny blocking WebSocket client speaking to a local DevTools endpoint."""

    def __init__(self, host: str, port: int, path: str, connect_timeout: float = 5.0):
        self.host = host
        self.port = int(port)
        self.path = path
        self.connect_timeout = connect_timeout
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._closed = False

    def connect(self) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        self.sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("connection closed during WebSocket handshake")
            head += chunk
        header, _, self._buf = head.partition(b"\r\n\r\n")
        lines = header.decode("latin-1").split("\r\n")
        if not lines or " 101 " not in lines[0].replace("HTTP/1.1 ", "HTTP/1.1 "):
            raise RuntimeError(f"WebSocket upgrade refused: {lines[0] if lines else 'no status'}")
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if not any(line.lower().startswith("sec-websocket-accept:") and expected in line for line in lines[1:]):
            raise RuntimeError("WebSocket handshake accept key mismatch")

    # -- frame IO -----------------------------------------------------------

    def _read_exact(self, n: int) -> bytes:
        assert self.sock is not None
        data = self._buf
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise WebSocketClosed("socket closed mid-frame")
            data += chunk
        self._buf, out = data[n:], data[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        assert self.sock is not None
        mask = secrets.token_bytes(4)
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_frame(self) -> tuple[int, bool, bytes]:
        h = self._read_exact(2)
        fin = bool(h[0] & 0x80)
        opcode = h[0] & 0x0F
        masked = bool(h[1] & 0x80)
        length = h[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, fin, payload

    def recv_message(self, timeout: Optional[float] = None) -> str:
        """Read one text message; answer pings; raise on close."""
        assert self.sock is not None
        if timeout is not None:
            self.sock.settimeout(timeout)
        fragments: list[bytes] = []
        while True:
            opcode, fin, payload = self._recv_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except Exception:
                    pass
                self._closed = True
                raise WebSocketClosed("server sent close frame")
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", "replace")

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def drain(self, timeout: float = 0.2) -> Optional[str]:
        """Try to read one message within timeout; None when quiet."""
        try:
            return self.recv_message(timeout=timeout)
        except (socket.timeout, TimeoutError):
            return None
        except (WebSocketClosed, ConnectionError, OSError):
            raise

    def close(self) -> None:
        if self.sock and not self._closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
            except Exception:
                pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None


# ---------------------------------------------------------------------------
# CDP injection worker
# ---------------------------------------------------------------------------


def _console_port() -> int:
    try:
        return int(os.environ.get("COURT_CONSOLE_PORT", CONSOLE_PORT_DEFAULT))
    except ValueError:
        return CONSOLE_PORT_DEFAULT


def _ws_connect(ws_url: str) -> WebSocketClient:
    # ws://127.0.0.1:PORT/devtools/page/<id> or /devtools/browser/<id>
    rest = ws_url.split("://", 1)[1]
    hostport, _, path = rest.partition("/")
    host, _, port_str = hostport.rpartition(":")
    if not host:
        host, port_str = port_str, "80"
    client = WebSocketClient(host or "127.0.0.1", int(port_str or 80), "/" + path)
    client.connect()
    return client


def _cdp_send(ws: WebSocketClient, msg_id: int, method: str, params: Optional[dict] = None) -> None:
    payload: dict[str, Any] = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    ws.send_text(json.dumps(payload))


def _inject_target(ws_url: str, script: str) -> WebSocketClient:
    """Connect to a page target, enable the page domain, pin the overlay
    script for future documents, and evaluate it in the current document."""
    ws = _ws_connect(ws_url)
    try:
        _cdp_send(ws, 1, "Page.enable")
        _cdp_send(ws, 2, "Page.addScriptToEvaluateOnNewDocument", {"source": script})
        _cdp_send(ws, 3, "Runtime.evaluate", {"expression": script, "returnByValue": False})
        # Drain the three acks (and any events) so the socket stays healthy.
        got = 0
        deadline = time.time() + 3.0
        while got < 3 and time.time() < deadline:
            if ws.drain(timeout=0.3) is not None:
                got += 1
    except Exception:
        ws.close()
        raise
    return ws


def _inject_worker(worktree: str) -> None:
    """Long-running overlay pinning loop.

    Runs until the state file is gone, /json/version stops answering, the
    stop-file appears, or the process is killed. Every poll cycle it
    re-lists page targets, injects new ones, re-injects dead WS sessions,
    and drains open sessions (ping/pong keep-alive).
    """
    endpoint = f"http://127.0.0.1:{_console_port()}/api/annotation"
    try:
        script = load_annotator_source(endpoint, str(worktree))
    except Exception as e:
        print(f"injector: cannot load annotator script: {e}", file=sys.stderr)
        return

    injected: dict[str, WebSocketClient] = {}
    port: Optional[int] = None

    def _drop(tid: str) -> None:
        ws = injected.pop(tid, None)
        if ws:
            ws.close()

    try:
        while True:
            try:
                if STOP_FILE_PATH.exists():
                    break
                state = _load_state()
                if not state:
                    break
                new_port = int(state.get("port") or 0)
                if new_port and new_port != port:
                    # Browser restarted on a different port: start fresh.
                    for tid in list(injected):
                        _drop(tid)
                    port = new_port
                if not port or not _pid_alive(state.get("pid")):
                    break
                if not cdp_responsive(port):
                    break

                targets = _http_get_json(f"http://127.0.0.1:{port}/json/list", timeout=3.0)
                if not isinstance(targets, list):
                    break
                live_ids: set[str] = set()
                for target in targets:
                    if target.get("type") != "page":
                        continue
                    tid = str(target.get("id") or "")
                    ws_url = target.get("webSocketDebuggerUrl") or ""
                    if not tid or not ws_url:
                        continue
                    live_ids.add(tid)
                    if tid in injected:
                        continue
                    try:
                        injected[tid] = _inject_target(ws_url, script)
                    except Exception:
                        continue

                for tid in list(injected):
                    if tid not in live_ids:
                        _drop(tid)
                        continue
                    try:
                        injected[tid].drain(timeout=0.05)
                    except Exception:
                        _drop(tid)

                time.sleep(POLL_SECONDS)
            except Exception:
                # Never crash the injector on one bad cycle; retry next poll.
                time.sleep(POLL_SECONDS)
    finally:
        for tid in list(injected):
            _drop(tid)
