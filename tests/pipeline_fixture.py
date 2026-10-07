"""Shared helpers: a capturing OTLP/HTTP endpoint and a reader for the cumulative metrics it received."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Collector:
    def __init__(self) -> None:
        self.payloads: list[dict] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                collector.payloads.append(json.loads(body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def latest(self) -> dict[tuple, float]:
        """Latest value per (metric, sorted labels); histograms report their count."""
        out: dict[tuple, float] = {}
        for payload in self.payloads:
            for rm in payload["resourceMetrics"]:
                for sm in rm["scopeMetrics"]:
                    for metric in sm["metrics"]:
                        body = metric.get("sum") or metric.get("histogram") or metric["gauge"]
                        for point in body["dataPoints"]:
                            labels = tuple(sorted((a["key"], a["value"]["stringValue"]) for a in point["attributes"]))
                            value = point.get("asInt", point.get("count", point.get("asDouble")))
                            out[(metric["name"], labels)] = float(value)
        return out

    def value(self, name: str, **labels: str) -> float | None:
        want = set(labels.items())
        found = [v for (n, lab), v in self.latest().items() if n == name and want <= set(lab)]
        return sum(found) if found else None
