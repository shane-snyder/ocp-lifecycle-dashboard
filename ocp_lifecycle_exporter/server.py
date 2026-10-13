"""Poll loop and /metrics endpoint.

Two independent cadences: the lifecycle API every few hours (it changes a few
times a month), the cluster every few minutes (an operator upgrade should show
up quickly). Metrics are re-rendered after either, and the last good lifecycle
payload is kept if a later fetch fails.
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

from .config import Config
from .kube import ClusterState, InstalledOperator, KubeClient
from .lifecycle import Catalog, fetch_catalog, utcnow
from .metrics import render_report
from .report import build_report

log = logging.getLogger(__name__)


class Exporter:
    def __init__(self, cfg: Config, kube: KubeClient):
        self.cfg = cfg
        self.kube = kube
        self.catalog: Optional[Catalog] = None
        self.cluster: Optional[ClusterState] = None
        self.operators: List[InstalledOperator] = []
        self.health: Dict[str, float] = {"api_failures": 0, "cluster_failures": 0}
        self._lock = threading.Lock()
        self._text = render_report(None, self.health)
        self._next_api = 0.0
        self._next_cluster = 0.0

    @property
    def text(self) -> str:
        with self._lock:
            return self._text

    def refresh_api(self) -> None:
        self.health["api_last_attempt"] = time.time()
        try:
            self.catalog = fetch_catalog(self.cfg.api_url)
        except Exception:
            self.health["api_failures"] += 1
            self._next_api = time.time() + self.cfg.retry_interval
            log.exception("lifecycle API fetch failed, retrying in %ss", self.cfg.retry_interval)
            return
        self.health["api_last_success"] = time.time()
        self.health["api_products"] = len(self.catalog.products)
        self._next_api = time.time() + self.cfg.poll_interval
        log.info("loaded %d products from lifecycle API", len(self.catalog.products))

    def refresh_cluster(self) -> None:
        self._next_cluster = time.time() + self.cfg.cluster_interval
        try:
            cluster = self.kube.cluster_state(self.cfg.target_ocp_version)
            operators = self.kube.installed_operators()
        except Exception:
            # Keep the last good snapshot whole; a half-updated one would
            # publish the new cluster with no operators.
            self.health["cluster_failures"] += 1
            self._next_cluster = time.time() + min(self.cfg.retry_interval, self.cfg.cluster_interval)
            log.exception("reading cluster state failed")
            return
        self.cluster, self.operators = cluster, operators
        self.health["cluster_last_success"] = time.time()
        log.info("cluster %s (channel %s), %d subscriptions",
                 self.cluster.version, self.cluster.channel, len(self.operators))

    def render(self) -> None:
        report = None
        if self.catalog:
            report = build_report(self.catalog, self.cluster, self.operators, utcnow(),
                                  self.cfg.ocp_product, self.cfg.overrides, self.cfg.warn_days)
        text = render_report(report, self.health)
        with self._lock:
            self._text = text

    def run_forever(self) -> None:
        while True:
            now = time.time()
            changed = False
            if now >= self._next_api:
                self.refresh_api()
                changed = True
            if now >= self._next_cluster:
                self.refresh_cluster()
                changed = True
            if changed:
                try:
                    self.render()
                except Exception:
                    log.exception("rendering metrics failed")
            time.sleep(max(1.0, min(self._next_api, self._next_cluster) - time.time()))


def serve(cfg: Config, exporter: Exporter) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if self.path.split("?")[0] == "/metrics":
                body = exporter.text.encode()
                ctype = "text/plain; version=0.0.4; charset=utf-8"
            elif self.path in ("/healthz", "/readyz"):
                body, ctype = b"ok\n", "text/plain"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    httpd = ThreadingHTTPServer((cfg.listen_addr, cfg.listen_port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("listening on %s:%d", cfg.listen_addr, cfg.listen_port)
    return httpd
