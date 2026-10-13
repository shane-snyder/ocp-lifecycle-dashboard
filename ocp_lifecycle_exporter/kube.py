"""Minimal read-only Kubernetes client for the three things we need.

Standard library only, so the exporter has no dependencies to pin or patch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .lifecycle import parse_version

log = logging.getLogger(__name__)

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

_CHANNEL_MINOR_RE = re.compile(r"(\d+\.\d+)$")
_CSV_NAME_VERSION_RE = re.compile(r"\.v?(\d+\.\d+(?:\.\d+)?[^\s]*)$")


@dataclass
class ClusterState:
    version: str
    desired_version: str
    channel: str
    target_minor: Optional[str]


@dataclass
class InstalledOperator:
    namespace: str
    subscription: str
    package: str
    channel: str
    source: str
    csv: str
    version: Optional[str]


class KubeClient:
    def __init__(self, api_url: Optional[str] = None, token: Optional[str] = None,
                 insecure: bool = False, ca_file: Optional[str] = None, timeout: float = 30.0):
        self._token = token
        self.timeout = timeout
        if api_url:
            self.api_url = api_url.rstrip("/")
        else:
            host = os.environ["KUBERNETES_SERVICE_HOST"]
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            self.api_url = f"https://{host}:{port}"
            if ca_file is None and os.path.exists(f"{SA_DIR}/ca.crt"):
                ca_file = f"{SA_DIR}/ca.crt"
        if insecure:
            self._ctx = ssl._create_unverified_context()  # noqa: S323 - opt-in for local dev only
        else:
            self._ctx = ssl.create_default_context(cafile=ca_file)

    def _bearer(self) -> str:
        if self._token:
            return self._token
        # Bound service account tokens rotate, so read the file on every call.
        with open(f"{SA_DIR}/token") as f:
            return f.read().strip()

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        url = self.api_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._bearer()}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
            return json.load(resp)

    def cluster_state(self, target_override: Optional[str] = None) -> ClusterState:
        cv = self.get("/apis/config.openshift.io/v1/clusterversions/version")
        status = cv.get("status") or {}
        desired = (status.get("desired") or {}).get("version") or ""
        # During an upgrade "desired" is where we are going, not where we are.
        current = next(
            (h.get("version") for h in status.get("history") or [] if h.get("state") == "Completed"),
            desired,
        )
        channel = (cv.get("spec") or {}).get("channel") or ""
        m = _CHANNEL_MINOR_RE.search(channel)
        target = target_override or (m.group(1) if m else None)
        return ClusterState(version=current, desired_version=desired, channel=channel, target_minor=target)

    def installed_operators(self) -> List[InstalledOperator]:
        subs = self.get("/apis/operators.coreos.com/v1alpha1/subscriptions").get("items") or []
        out = []
        for s in subs:
            meta, spec, status = s.get("metadata") or {}, s.get("spec") or {}, s.get("status") or {}
            ns, pkg = meta.get("namespace", ""), spec.get("name", "")
            csv, version = self._live_csv(ns, status.get("installedCSV") or "")
            out.append(InstalledOperator(
                namespace=ns,
                subscription=meta.get("name", ""),
                package=pkg,
                channel=spec.get("channel") or "",
                source=spec.get("source") or "",
                csv=csv,
                version=version,
            ))
        return out

    def _live_csv(self, namespace: str, csv: str) -> Tuple[str, Optional[str]]:
        """The installed CSV's name and version."""
        if not csv:
            return "", None
        base = f"/apis/operators.coreos.com/v1alpha1/namespaces/{namespace}/clusterserviceversions"
        try:
            return csv, (self.get(f"{base}/{csv}").get("spec") or {}).get("version") or _version_from_csv_name(csv)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        # status.installedCSV can lag behind an automatic upgrade and name a CSV
        # that has already been replaced (seen with kiali-operator). Find the
        # successor by its name prefix among the namespace's own CSVs.
        prefix = _csv_name_prefix(csv)
        items = self.get(base, {"labelSelector": "!olm.copiedFrom"}).get("items") or []
        live = [
            c for c in items
            if _csv_name_prefix(c["metadata"]["name"]) == prefix
            and (c.get("status") or {}).get("phase") == "Succeeded"
        ]
        if live:
            newest = max(live, key=lambda c: parse_version(_version_from_csv_name(c["metadata"]["name"])) or ())
            name = newest["metadata"]["name"]
            return name, (newest.get("spec") or {}).get("version") or _version_from_csv_name(name)
        log.info("CSV %s/%s not found, using version from its name", namespace, csv)
        return csv, _version_from_csv_name(csv)


def _version_from_csv_name(csv: str) -> Optional[str]:
    """"aap-operator.v2.6.0-0.1787258175" -> "2.6.0-0.1787258175"."""
    m = _CSV_NAME_VERSION_RE.search(csv)
    return m.group(1) if m else None


def _csv_name_prefix(csv: str) -> str:
    """"kiali-operator.v2.22.1" -> "kiali-operator"."""
    m = _CSV_NAME_VERSION_RE.search(csv)
    return csv[: m.start()] if m else csv
