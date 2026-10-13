"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional

from .lifecycle import DEFAULT_API_URL

# OLM packages the lifecycle API does not list under ``package`` (or lists
# under a different name). Maps package -> lifecycle product name.
DEFAULT_PACKAGE_OVERRIDES: Dict[str, str] = {
    "lightspeed-operator": "Red Hat OpenShift Lightspeed",
    "servicemeshoperator3": "Red Hat OpenShift Service Mesh",
    "devworkspace-operator": "DevWorkspace Operator",
}


def _parse_overrides(text: str) -> Dict[str, str]:
    """"pkg-a=Product A;pkg-b=Product B" -> {...}. Semicolons because product names contain commas."""
    out = {}
    for item in text.split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def _bool(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    api_url: str = DEFAULT_API_URL
    poll_interval: int = 4 * 3600
    retry_interval: int = 300
    cluster_interval: int = 600
    listen_addr: str = "0.0.0.0"
    listen_port: int = 9120
    ocp_product: str = "Red Hat OpenShift Container Platform"
    warn_days: int = 90
    target_ocp_version: Optional[str] = None
    overrides: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PACKAGE_OVERRIDES))
    kube_api_url: Optional[str] = None
    kube_token: Optional[str] = None
    kube_insecure: bool = False

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        c = cls()
        c.api_url = env.get("LIFECYCLE_API_URL", c.api_url)
        c.poll_interval = int(env.get("POLL_INTERVAL_SECONDS", c.poll_interval))
        c.retry_interval = int(env.get("RETRY_INTERVAL_SECONDS", c.retry_interval))
        c.cluster_interval = int(env.get("CLUSTER_REFRESH_SECONDS", c.cluster_interval))
        c.listen_addr = env.get("LISTEN_ADDR", c.listen_addr)
        c.listen_port = int(env.get("LISTEN_PORT", c.listen_port))
        c.ocp_product = env.get("OCP_PRODUCT_NAME", c.ocp_product)
        c.warn_days = int(env.get("WARN_DAYS", c.warn_days))
        c.target_ocp_version = env.get("TARGET_OCP_VERSION") or None
        c.overrides.update(_parse_overrides(env.get("PACKAGE_PRODUCT_OVERRIDES", "")))
        c.kube_api_url = env.get("KUBE_API_URL") or None
        c.kube_token = env.get("KUBE_TOKEN") or None
        c.kube_insecure = _bool(env.get("KUBE_INSECURE_SKIP_TLS_VERIFY"))
        return c
