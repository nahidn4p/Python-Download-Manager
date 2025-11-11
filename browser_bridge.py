import json
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty
from typing import Dict, List, Optional, Tuple


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17894
MAX_CONTENT_LENGTH = 512 * 1024  # 512 KB payload limit


class _BridgeRequestHandler(BaseHTTPRequestHandler):
    bridge = None  # type: Optional["BrowserBridge"]

    def log_message(self, format: str, *args):
        # suppress default logging (integrate with bridge logger if needed)
        if self.bridge and self.bridge.verbose:
            super().log_message(format, *args)

    def _send_json(
        self,
        payload: Dict,
        status: HTTPStatus = HTTPStatus.OK,
        headers: Optional[Dict[str, str]] = None,
    ):
        response = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(response)))
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(response)

    def _parse_json_body(self) -> Tuple[Optional[Dict], Optional[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return None, "Empty request body"
        if length > MAX_CONTENT_LENGTH:
            return None, "Request body too large"

        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                return None, "JSON body must be an object"
            return data, None
        except json.JSONDecodeError:
            return None, "Invalid JSON"

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_POST(self):
        if not self.bridge:
            self._send_json({"ok": False, "error": "Bridge not ready"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return

        path = self.path.rstrip("/")

        if path == "/enqueue":
            body, error = self._parse_json_body()
            if error:
                self._send_json({"ok": False, "error": error}, HTTPStatus.BAD_REQUEST)
                return

            url = (body.get("url") or "").strip()
            filename = (body.get("filename") or "").strip()
            headers = body.get("headers") or {}

            if not url:
                self._send_json({"ok": False, "error": "Missing url"}, HTTPStatus.BAD_REQUEST)
                return

            try:
                self.bridge.enqueue_request(
                    {
                        "kind": "download",
                        "url": url,
                        "filename": filename or None,
                        "headers": headers if isinstance(headers, dict) else {},
                    }
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            self._send_json({"ok": True})
            return

        if path == "/enqueue-media":
            body, error = self._parse_json_body()
            if error:
                self._send_json({"ok": False, "error": error}, HTTPStatus.BAD_REQUEST)
                return

            url = (body.get("manifest_url") or "").strip()
            media_type = (body.get("media_type") or "hls").strip().lower()
            source = (body.get("source_url") or "").strip()
            title = (body.get("title") or "").strip()
            headers = body.get("headers") or {}

            if not url:
                self._send_json({"ok": False, "error": "Missing manifest_url"}, HTTPStatus.BAD_REQUEST)
                return

            try:
                self.bridge.enqueue_request(
                    {
                        "kind": "media",
                        "manifest_url": url,
                        "media_type": media_type,
                        "source_url": source or None,
                        "title": title or None,
                        "headers": headers if isinstance(headers, dict) else {},
                    }
                )
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            self._send_json({"ok": True})
            return

        self._send_json({"ok": False, "error": "Unknown endpoint"}, HTTPStatus.NOT_FOUND)


class BrowserBridge:
    """
    Lightweight HTTP bridge so browser extensions can hand URLs to the app.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, verbose: bool = False):
        self.host = host
        self.port = port
        self.verbose = verbose
        self._queue: "Queue[Dict]" = Queue()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        server_address = (self.host, self._find_open_port(self.port))

        _BridgeRequestHandler.bridge = self
        self._server = ThreadingHTTPServer(server_address, _BridgeRequestHandler)
        self._server.daemon_threads = True

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._serve, name="BrowserBridgeServer", daemon=True)
        self._thread.start()

        if self.verbose:
            print(f"[BrowserBridge] Listening on http://{server_address[0]}:{server_address[1]}")

    def _serve(self):
        try:
            while not self._stop_event.is_set():
                self._server.handle_request()
        except Exception as exc:  # pragma: no cover - defensive
            if self.verbose:
                print(f"[BrowserBridge] Server stopped: {exc}")

    def stop(self):
        self._stop_event.set()
        if self._server:
            try:
                self._server.server_close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._server = None
        self._thread = None

    def enqueue_request(self, payload: Dict):
        self._queue.put(payload, block=False)

    def poll_requests(self, limit: int = 20) -> List[Dict]:
        items: List[Dict] = []
        for _ in range(limit):
            try:
                items.append(self._queue.get_nowait())
            except Empty:
                break
        return items

    def resolve_server_address(self) -> Optional[Tuple[str, int]]:
        if not self._server:
            return None
        return self._server.server_address

    def _find_open_port(self, preferred: int) -> int:
        if self._port_available(preferred):
            return preferred
        for offset in range(1, 100):
            candidate = preferred + offset
            if self._port_available(candidate):
                if self.verbose:
                    print(f"[BrowserBridge] Port {preferred} in use, falling back to {candidate}")
                return candidate
        raise RuntimeError("No open port available for BrowserBridge")

    def _port_available(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, port))
            except OSError:
                return False
        return True


__all__ = ["BrowserBridge", "DEFAULT_HOST", "DEFAULT_PORT"]

