"""Renders a Report as Prometheus text exposition.

Label names avoid ``namespace``, ``cluster`` and ``version`` collisions with
labels added by Prometheus and ACM: operator location is ``operator_namespace``
and ACM attaches ``cluster`` itself when it forwards these to the hub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .report import Report

PREFIX = "ocp_lifecycle_"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(float(value))


@dataclass
class Family:
    name: str
    help: str
    type: str = "gauge"
    samples: List[Tuple[Dict[str, str], float]] = field(default_factory=list)

    def add(self, value: Optional[float], **labels: Optional[str]) -> None:
        if value is None:
            return
        self.samples.append(({k: v or "" for k, v in labels.items()}, value))


class Registry:
    def __init__(self) -> None:
        self._families: Dict[str, Family] = {}

    def family(self, name: str, help_text: str, type_: str = "gauge") -> Family:
        full = PREFIX + name
        if full not in self._families:
            self._families[full] = Family(full, help_text, type_)
        return self._families[full]

    def render(self) -> str:
        lines: List[str] = []
        for fam in self._families.values():
            lines.append(f"# HELP {fam.name} {fam.help}")
            lines.append(f"# TYPE {fam.name} {fam.type}")
            for labels, value in fam.samples:
                if labels:
                    body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
                    lines.append(f"{fam.name}{{{body}}} {_fmt(value)}")
                else:
                    lines.append(f"{fam.name} {_fmt(value)}")
        return "\n".join(lines) + "\n"


def _ts(dt: Optional[datetime]) -> Optional[float]:
    return dt.timestamp() if dt else None


def render_report(report: Optional[Report], health: Dict[str, float]) -> str:
    r = Registry()

    r.family("api_last_success_timestamp_seconds",
             "Last successful fetch of the Red Hat Product Life Cycle API.").add(health.get("api_last_success"))
    r.family("api_last_attempt_timestamp_seconds",
             "Last attempted fetch of the Red Hat Product Life Cycle API.").add(health.get("api_last_attempt"))
    r.family("api_fetch_failures_total", "Failed fetches of the Red Hat Product Life Cycle API.",
             "counter").add(health.get("api_failures", 0))
    r.family("api_products", "Products in the last lifecycle payload.").add(health.get("api_products"))
    r.family("cluster_last_success_timestamp_seconds",
             "Last successful read of ClusterVersion and Subscriptions.").add(health.get("cluster_last_success"))
    r.family("cluster_read_failures_total", "Failed reads of ClusterVersion and Subscriptions.",
             "counter").add(health.get("cluster_failures", 0))

    if report is None:
        return r.render()

    # --- OpenShift lifecycle reference -------------------------------------
    ocp_phase = r.family("ocp_version_support_phase",
                         "Current support phase of an OpenShift minor: 3 full, 2 maintenance, 1 extended, 0 EOL, -1 unknown.")
    ocp_phase_end = r.family("ocp_version_phase_end_timestamp_seconds",
                             "End date of each lifecycle phase of an OpenShift minor.")
    ocp_support_end = r.family("ocp_version_support_end_timestamp_seconds",
                               "End of standard (full + maintenance) support for an OpenShift minor.")
    ocp_ext_end = r.family("ocp_version_extended_support_end_timestamp_seconds",
                           "End of the last extended (EUS/ELS) phase for an OpenShift minor.")
    for o in report.ocp_versions:
        lc = o.lifecycle
        ocp_phase.add(lc.phase_code, ocp_version=o.version, phase=lc.phase_name)
        for name, end in lc.phase_ends.items():
            ocp_phase_end.add(end.timestamp(), ocp_version=o.version, phase=name)
        ocp_support_end.add(_ts(lc.support_end), ocp_version=o.version)
        ocp_ext_end.add(_ts(lc.extended_support_end), ocp_version=o.version)

    # --- This cluster ------------------------------------------------------
    c = report.cluster
    if c:
        r.family("cluster_info", "OpenShift version and update channel of this cluster.").add(
            1, ocp_version=c.minor, version=c.state.version, desired_version=c.state.desired_version,
            channel=c.state.channel, target_ocp_version=c.state.target_minor)
        r.family("cluster_status", "Lifecycle status of this cluster's OpenShift minor: 2 critical, 1 warning, 0 ok, -1 unknown.").add(
            c.severity, ocp_version=c.minor, status=c.status)
        if c.lifecycle:
            lc = c.lifecycle
            r.family("cluster_support_phase", "Support phase of this cluster's OpenShift minor (see ocp_version_support_phase).").add(
                lc.phase_code, ocp_version=c.minor, phase=lc.phase_name)
            r.family("cluster_current_phase_end_timestamp_seconds", "When the cluster's current support phase ends.").add(
                _ts(lc.current_phase_end), ocp_version=c.minor, phase=lc.phase_name)
            r.family("cluster_support_end_timestamp_seconds", "End of standard support for this cluster's OpenShift minor.").add(
                _ts(lc.support_end), ocp_version=c.minor)
            r.family("cluster_extended_support_end_timestamp_seconds", "End of extended support for this cluster's OpenShift minor.").add(
                _ts(lc.extended_support_end), ocp_version=c.minor)

    # --- Installed operators -----------------------------------------------
    info = r.family("operator_info", "One series per installed Subscription with its lifecycle match.")
    status = r.family("operator_status",
                      "Lifecycle status of an installed operator: 2 critical, 1 warning, 0 ok, -1 unknown/not tracked.")
    phase = r.family("operator_support_phase",
                     "Support phase of the installed operator version: 3 full, 2 maintenance, 1 extended, 0 EOL.")
    phase_end = r.family("operator_current_phase_end_timestamp_seconds",
                         "When the installed operator version's current support phase ends.")
    support_end = r.family("operator_support_end_timestamp_seconds",
                           "End of standard support for the installed operator version.")
    ext_end = r.family("operator_extended_support_end_timestamp_seconds",
                       "End of extended support for the installed operator version.")
    compat_now = r.family("operator_compatible_with_cluster",
                          "Installed operator version supports this cluster's OpenShift minor: 1 yes, 0 no, -1 unknown.")
    compat_target = r.family("operator_compatible_with_target",
                             "Installed operator version supports the OpenShift minor of the update channel: 1 yes, 0 no, -1 unknown.")
    matrix = r.family("operator_ocp_compatibility",
                      "One series per OpenShift minor the installed operator version supports.")
    rec = r.family("operator_recommended_version_info",
                   "Newest supported operator version compatible with the cluster (for=current) or channel target (for=target).")

    for o in report.operators:
        op = o.op
        ids = dict(operator_namespace=op.namespace, package=op.package)
        lifecycle_version = o.version.name if o.version else ""
        info.add(1, **ids, subscription=op.subscription, csv=op.csv, installed_version=op.version,
                 channel=op.channel, source=op.source,
                 product=o.product.name if o.product else "",
                 lifecycle_version=lifecycle_version,
                 tracked="true" if o.product else "false",
                 phase=o.lifecycle.phase_name if o.lifecycle else "",
                 compatible_ocp_versions=", ".join(o.version.ocp_compat) if o.version else "",
                 link=o.product.link if o.product else "")
        status.add(o.severity, **ids, status=o.status, reasons=",".join(o.reasons))
        if o.product is None:
            continue
        compat_now.add(o.compatible_now, **ids, ocp_version=report.cluster.minor if report.cluster else "")
        compat_target.add(o.compatible_target, **ids,
                          target_ocp_version=report.cluster.state.target_minor if report.cluster else "")
        for which, v in (("current", o.recommended_now), ("target", o.recommended_target)):
            if v:
                cluster = report.cluster
                for_ocp = (cluster.minor if which == "current" else cluster.state.target_minor) if cluster else ""
                rec.add(1, **ids, **{"for": which}, ocp_version=for_ocp, recommended_version=v.name)
        if o.lifecycle is None:
            continue
        lc = o.lifecycle
        phase.add(lc.phase_code, **ids, lifecycle_version=lifecycle_version, phase=lc.phase_name)
        phase_end.add(_ts(lc.current_phase_end), **ids, phase=lc.phase_name)
        support_end.add(_ts(lc.support_end), **ids)
        ext_end.add(_ts(lc.extended_support_end), **ids)
        for ocp in o.version.ocp_compat:
            matrix.add(1, **ids, lifecycle_version=lifecycle_version, ocp_version=ocp)

    return r.render()
