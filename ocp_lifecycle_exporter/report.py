"""Joins the cluster's installed state against the lifecycle catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .kube import ClusterState, InstalledOperator
from .lifecycle import (
    PHASE_EOL,
    PHASE_LABELS,
    PHASE_UNKNOWN,
    Catalog,
    Phase,
    Product,
    ProductVersion,
    iter_ocp_versions,
    ocp_minor,
)

# Status severities. The status label says why; the value says how bad.
SEVERITY_UNKNOWN = -1
SEVERITY_OK = 0
SEVERITY_WARNING = 1
SEVERITY_CRITICAL = 2

COMPAT_UNKNOWN = -1


@dataclass
class VersionLifecycle:
    phase_code: int
    phase_name: str
    current_phase_end: Optional[datetime]
    support_end: Optional[datetime]
    extended_support_end: Optional[datetime]
    phase_ends: Dict[str, datetime]

    @classmethod
    def of(cls, v: ProductVersion, now: datetime) -> "VersionLifecycle":
        code, window = v.current_phase(now)
        return cls(
            phase_code=code,
            phase_name=_phase_name(code, window),
            current_phase_end=window.end if window else None,
            support_end=v.support_end(),
            extended_support_end=v.extended_support_end(),
            phase_ends={p.name: p.end for p in v.phases if p.end and p.category is not None},
        )


@dataclass
class OcpVersionReport:
    version: str
    lifecycle: VersionLifecycle


@dataclass
class ClusterReport:
    state: ClusterState
    minor: Optional[str]
    lifecycle: Optional[VersionLifecycle]
    status: str
    severity: int


@dataclass
class OperatorReport:
    op: InstalledOperator
    product: Optional[Product]
    version: Optional[ProductVersion]
    lifecycle: Optional[VersionLifecycle]
    compatible_now: int
    compatible_target: int
    recommended_now: Optional[ProductVersion]
    recommended_target: Optional[ProductVersion]
    status: str
    severity: int
    reasons: List[str] = field(default_factory=list)


@dataclass
class Report:
    ocp_versions: List[OcpVersionReport]
    cluster: Optional[ClusterReport]
    operators: List[OperatorReport]


def _phase_name(code: int, window: Optional[Phase]) -> str:
    # Prefer the API's own wording ("Extended update support Term 2") when we
    # have the window; fall back to the generic category label.
    return window.name if window else PHASE_LABELS.get(code, "Unknown")


def _compat(version: Optional[ProductVersion], ocp: Optional[str]) -> int:
    if version is None or not ocp or not version.ocp_compat:
        return COMPAT_UNKNOWN
    return 1 if ocp in version.ocp_compat else 0


def build_report(catalog: Catalog, cluster: Optional[ClusterState], operators: List[InstalledOperator],
                 now: datetime, ocp_product: str, overrides: Dict[str, str], warn_days: int) -> Report:
    warn_before = now + timedelta(days=warn_days)
    ocp = catalog.product(ocp_product)

    ocp_versions: List[OcpVersionReport] = []
    cluster_minor = ocp_minor(cluster.version) if cluster else None
    target_minor = cluster.target_minor if cluster else None
    if ocp:
        for v in iter_ocp_versions(ocp):
            lc = VersionLifecycle.of(v, now)
            # Every supported minor, plus the ones this cluster is on or heading to
            # even if they have aged out, so the dashboard never loses its own row.
            if lc.phase_code > PHASE_EOL or v.name in (cluster_minor, target_minor):
                ocp_versions.append(OcpVersionReport(version=v.name, lifecycle=lc))

    cluster_report = None
    if cluster:
        cluster_lc = next((o.lifecycle for o in ocp_versions if o.version == cluster_minor), None)
        status, severity = _lifecycle_status(cluster_lc, warn_before)
        cluster_report = ClusterReport(cluster, cluster_minor, cluster_lc, status, severity)

    op_reports = [
        _operator_report(catalog, op, cluster_minor, target_minor, now, overrides, warn_before)
        for op in operators
    ]
    return Report(ocp_versions=ocp_versions, cluster=cluster_report, operators=op_reports)


def _lifecycle_status(lc: Optional[VersionLifecycle], warn_before: datetime):
    if lc is None or lc.phase_code == PHASE_UNKNOWN:
        return "unknown", SEVERITY_UNKNOWN
    if lc.phase_code == PHASE_EOL:
        return "end_of_life", SEVERITY_CRITICAL
    end = lc.extended_support_end or lc.support_end
    if end and end <= warn_before:
        return "support_ending_soon", SEVERITY_WARNING
    return "ok", SEVERITY_OK


def _is_platform_aligned(product: Product, version: ProductVersion, cluster_minor: Optional[str],
                         target_minor: str) -> bool:
    """Operator version tracks the OpenShift minor and a release for the target exists."""
    return (
        version.name == cluster_minor
        and any(v.name == target_minor and target_minor in v.ocp_compat for v in product.versions)
    )


def _operator_report(catalog: Catalog, op: InstalledOperator, cluster_minor: Optional[str],
                     target_minor: Optional[str], now: datetime, overrides: Dict[str, str],
                     warn_before: datetime) -> OperatorReport:
    product = catalog.product_for_package(op.package, overrides)
    version = product.match_version(op.version) if product else None
    lc = VersionLifecycle.of(version, now) if version else None
    compat_now = _compat(version, cluster_minor)
    compat_target = _compat(version, target_minor)
    rec_now = product.newest_supported_for(cluster_minor, now) if product and cluster_minor else None
    rec_target = product.newest_supported_for(target_minor, now) if product and target_minor else None

    reasons: List[str] = []
    severity = SEVERITY_OK
    if product is None:
        return OperatorReport(op, None, None, None, compat_now, compat_target, None, None,
                              "not_tracked", SEVERITY_UNKNOWN)
    if version is None or lc is None:
        return OperatorReport(op, product, None, None, compat_now, compat_target, rec_now, rec_target,
                              "unknown_version", SEVERITY_UNKNOWN)

    # Ordered most to least severe; the first one becomes the headline status.
    if lc.phase_code == PHASE_EOL:
        reasons.append("end_of_life")
    if compat_now == 0:
        reasons.append("incompatible_with_cluster")
    if reasons:
        severity = SEVERITY_CRITICAL
    notes: List[str] = []
    if target_minor and target_minor != cluster_minor and compat_target == 0:
        if _is_platform_aligned(product, version, cluster_minor, target_minor):
            # Versioned in lockstep with OpenShift (LVMS 4.20 on OCP 4.20):
            # updated at each hop of the cluster upgrade, not ahead of it.
            notes.append("update_with_cluster")
        else:
            reasons.append("blocks_cluster_upgrade")
    if lc.phase_code > PHASE_EOL:
        # An operator's own EUS only counts if it lasts; use the furthest date.
        end = lc.extended_support_end or lc.support_end
        if end and end <= warn_before:
            reasons.append("support_ending_soon")
    if reasons and severity == SEVERITY_OK:
        severity = SEVERITY_WARNING
    if rec_now and rec_now.key and version.key and rec_now.key > version.key:
        notes.append("newer_version_available")
    # Notes are informational: they never raise severity, and only become the
    # headline status when nothing is actually wrong.
    reasons += notes
    status = reasons[0] if reasons else "ok"
    return OperatorReport(op, product, version, lc, compat_now, compat_target, rec_now, rec_target,
                          status, severity, reasons)
