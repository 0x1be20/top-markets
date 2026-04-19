from __future__ import annotations

import json
import logging
import mimetypes
import signal
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from market_pool import MarketPoolService


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SERVICE = MarketPoolService(BASE_DIR)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "TopMarkets/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/state":
            self._send_json(SERVICE.get_dashboard())
            return
        if parsed.path == "/api/export/tradingview":
            watchlist_text = SERVICE.get_tradingview_watchlist_text()
            file_name = f"candidate-pool-tradingview-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
            self._send_text(
                watchlist_text,
                content_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
            )
            return
        if parsed.path == "/":
            self._serve_file(STATIC_DIR / "index.html")
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run-update":
            result = SERVICE.run_update(trigger="manual")
            self._send_json({"ok": True, "result": result})
            return
        if parsed.path == "/api/backfill":
            params = parse_qs(parsed.query)
            days = int(params.get("days", ["7"])[0])
            include_now = params.get("include_now", ["1"])[0] != "0"
            result = SERVICE.backfill_days(days=days, include_now=include_now)
            self._send_json({"ok": True, "result": result})
            return
        self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logging.info("%s - %s", self.address_string(), format % args)

    def _serve_static(self, raw_path: str) -> None:
        rel_path = raw_path.lstrip("/")
        target = (STATIC_DIR / rel_path).resolve()
        if STATIC_DIR not in target.parents and target != STATIC_DIR:
            self._send_json({"error": "invalid_path"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not target.exists() or not target.is_file():
            self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._serve_file(target)

    def _serve_file(self, path: Path) -> None:
        mime_type, _ = mimetypes.guess_type(path.name)
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(
        self,
        payload: str,
        status: HTTPStatus = HTTPStatus.OK,
        content_type: str = "text/plain; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    port = 8008
    if len(sys.argv) > 1:
        query = parse_qs(urlparse(sys.argv[1]).query)
        if "port" in query:
            port = int(query["port"][0])
        else:
            port = int(sys.argv[1])

    SERVICE.start()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), AppHandler)
    logging.info("serving on http://127.0.0.1:%s", port)

    def shutdown_handler(signum, frame) -> None:  # noqa: ARG001
        logging.info("shutting down")
        SERVICE.stop()
        httpd.shutdown()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        httpd.serve_forever()
    finally:
        SERVICE.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
