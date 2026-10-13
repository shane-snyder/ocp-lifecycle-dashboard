"""Parsing and querying the Red Hat Product Life Cycle API.

The API behind https://access.redhat.com/product-life-cycles returns every
product with its versions, each version with a list of dated phases and, for
operators, the OpenShift minors it supports. Operators carry their OLM package
name(s) in ``package``, which is what lets us join against Subscriptions.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULT_API_URL = "https://access.redhat.com/product-life-cycles/api/v1/products"

# Numeric codes so Grafana can colour cells and alerts can compare values.
PHASE_FULL = 3
PHASE_MAINTENANCE = 2
PHASE_EXTENDED = 1
PHASE_EOL = 0
PHASE_UNKNOWN = -1

PHASE_LABELS = {
    PHASE_FULL: "Full support",
    PHASE_MAINTENANCE: "Maintenance support",
    PHASE_EXTENDED: "Extended support",
    PHASE_EOL: "End of life",
    PHASE_UNKNOWN: "Unknown",
}

# Fallback when the dated phases do not cover "now" (missing or textual dates).
_TYPE_CODES = {
    "full support": PHASE_FULL,
    "maintenance support": PHASE_MAINTENANCE,
    "extended support": PHASE_EXTENDED,
    "end of life": PHASE_EOL,
    "end of maintenance": PHASE_EOL,
}

VersionKey = Tuple[int, ...]

_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)")
_OCP_MINOR_RE = re.compile(r"\b(\d+\.\d+)\b")


def phase_category(name: str) -> Optional[int]:
    """Map a phase name to a support category, or None for date markers.

    Names seen in the API include "Maintenance Support 1", "Extended update
    support Term 2" and "Extended life cycle support (ELS) add-on", so this
    matches on prefix rather than exact name.
    """
    n = name.strip().lower()
    if n.startswith("full support"):
        return PHASE_FULL
    if n.startswith("maintenance support"):
        return PHASE_MAINTENANCE
    if n.startswith("extended"):
        return PHASE_EXTENDED
    # "General availability" and "End of Life" are single dates, not windows.
    return None


def parse_version(text: Optional[str]) -> Optional[VersionKey]:
    """Leading numeric dotted version: "v4.20.0-2026..." -> (4, 20, 0), "7.x" -> (7,)."""
    if not text:
        return None
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def ocp_minor(version: Optional[str]) -> Optional[str]:
    key = parse_version(version)
    if not key or len(key) < 2:
        return None
    return f"{key[0]}.{key[1]}"


def parse_ocp_list(text: Optional[str]) -> List[str]:
    """"4.19, 4.20, 4.21" -> ["4.19", "4.20", "4.21"]; "N/A" or None -> []."""
    if not text:
        return []
    return _OCP_MINOR_RE.findall(text)


def _parse_date(value: Optional[str], fmt: Optional[str]) -> Optional[datetime]:
    if fmt != "date" or not value or value == "N/A":
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Phase:
    name: str
    category: Optional[int]
    start: Optional[datetime]
    end: Optional[datetime]


@dataclass
class ProductVersion:
    name: str
    key: Optional[VersionKey]
    type: str
    phases: List[Phase]
    ocp_compat: List[str]

    def support_end(self) -> Optional[datetime]:
        """End of the last full/maintenance window (standard support)."""
        ends = [p.end for p in self.phases if p.end and p.category in (PHASE_FULL, PHASE_MAINTENANCE)]
        if ends:
            return max(ends)
        eol = [p.end for p in self.phases if p.end and p.name.strip().lower() == "end of life"]
        return max(eol) if eol else None

    def extended_support_end(self) -> Optional[datetime]:
        ends = [p.end for p in self.phases if p.end and p.category == PHASE_EXTENDED]
        return max(ends) if ends else None

    def current_phase(self, now: datetime) -> Tuple[int, Optional[Phase]]:
        """The support category in effect at ``now`` and the phase window providing it.

        Red Hat publishes end dates as midnight UTC of the last supported day,
        so each window is treated as inclusive of that whole day.
        """
        windows = [p for p in self.phases if p.category is not None and p.end is not None]
        for p in windows:
            if p.end + timedelta(days=1) > now and (p.start is None or p.start <= now):
                return p.category, p
        if windows and all(p.end + timedelta(days=1) <= now for p in windows):
            return PHASE_EOL, None
        return _TYPE_CODES.get(self.type.strip().lower(), PHASE_UNKNOWN), None


@dataclass
class Product:
    name: str
    packages: List[str]
    link: Optional[str]
    is_operator: bool
    versions: List[ProductVersion] = field(default_factory=list)

    def match_version(self, installed: Optional[str]) -> Optional[ProductVersion]:
        """The lifecycle entry that covers an installed operator version.

        Lifecycle entries are usually "major.minor" but some are "1.5.0",
        "2.19.1" or "7.x". Preference order: the longest entry that is a prefix
        of the installed version, then an entry with the same major.minor
        (so "2.19.0" still matches a "2.19.1" row).
        """
        inst = parse_version(installed)
        if not inst:
            return None
        best: Optional[ProductVersion] = None
        best_score = 0.0
        for v in self.versions:
            if not v.key:
                continue
            score = 0.0
            if inst[: len(v.key)] == v.key and (len(v.key) >= 2 or len(inst) == 1 or "x" in v.name.lower()):
                score = float(len(v.key))
            elif len(v.key) >= 2 and len(inst) >= 2 and v.key[:2] == inst[:2]:
                score = 1.5
            if score > best_score:
                best, best_score = v, score
        return best

    def newest_supported_for(self, ocp_version: str, now: datetime) -> Optional[ProductVersion]:
        """Newest version that is still supported and lists ``ocp_version`` as compatible."""
        candidates = [
            v
            for v in self.versions
            if v.key and ocp_version in v.ocp_compat and v.current_phase(now)[0] > PHASE_EOL
        ]
        return max(candidates, key=lambda v: v.key) if candidates else None


@dataclass
class Catalog:
    products: List[Product]
    by_package: Dict[str, Product]
    by_name: Dict[str, Product]

    def product_for_package(self, package: str, overrides: Dict[str, str]) -> Optional[Product]:
        override = overrides.get(package)
        if override:
            return self.by_name.get(override.lower())
        return self.by_package.get(package)

    def product(self, name: str) -> Optional[Product]:
        return self.by_name.get(name.lower())


def _parse_product(raw: dict) -> Product:
    packages = [p.strip() for p in (raw.get("package") or "").split(",") if p.strip()]
    versions = []
    for rv in raw.get("versions") or []:
        phases = [
            Phase(
                name=rp.get("name", ""),
                category=phase_category(rp.get("name", "")),
                start=_parse_date(rp.get("start_date"), rp.get("start_date_format")),
                end=_parse_date(rp.get("end_date"), rp.get("end_date_format")),
            )
            for rp in rv.get("phases") or []
        ]
        versions.append(
            ProductVersion(
                name=rv.get("name", ""),
                key=parse_version(rv.get("name")),
                type=rv.get("type") or "",
                phases=phases,
                ocp_compat=parse_ocp_list(rv.get("openshift_compatibility")),
            )
        )
    return Product(
        name=raw.get("name", ""),
        packages=packages,
        link=raw.get("link"),
        is_operator=bool(raw.get("is_operator")),
        versions=versions,
    )


def parse_catalog(payload: dict) -> Catalog:
    products = [_parse_product(p) for p in payload.get("data") or []]
    by_package: Dict[str, Product] = {}
    by_name: Dict[str, Product] = {}
    for p in products:
        by_name[p.name.lower()] = p
        for pkg in p.packages:
            by_package.setdefault(pkg, p)
    return Catalog(products=products, by_package=by_package, by_name=by_name)


def fetch_catalog(url: str = DEFAULT_API_URL, timeout: float = 60.0) -> Catalog:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ocp-lifecycle-exporter"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_catalog(json.load(resp))


def iter_ocp_versions(product: Product) -> Iterable[ProductVersion]:
    """OCP 4.x and later minors; OCP 3 and odd entries like "4.6 EUS" are skipped."""
    for v in product.versions:
        if v.key and len(v.key) == 2 and v.key[0] >= 4 and v.name == f"{v.key[0]}.{v.key[1]}":
            yield v


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
