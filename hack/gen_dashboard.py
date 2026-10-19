"""The OpenShift & operator lifecycle Grafana dashboard, as a dict.

    python3 hack/gen_dashboard.py [--datasource UID]   # dashboard JSON

hack/generate.py writes it, and the Grafana Operator and ACM wrappers around
it, into the repo.
"""
import argparse
import json
import re

DS = {"type": "prometheus", "uid": "${datasource}"}
C = 'cluster=~"$cluster"'

STATUS_MAP = [{"type": "value", "options": {
    "2": {"text": "Action required", "color": "red", "index": 0},
    "1": {"text": "Plan update", "color": "orange", "index": 1},
    "0": {"text": "OK", "color": "green", "index": 2},
    "-1": {"text": "Not tracked", "color": "text", "index": 3},
}}]
PHASE_MAP = [{"type": "value", "options": {
    "3": {"text": "Full support", "color": "green", "index": 0},
    "2": {"text": "Maintenance", "color": "blue", "index": 1},
    "1": {"text": "Extended (EUS)", "color": "orange", "index": 2},
    "0": {"text": "End of life", "color": "red", "index": 3},
    "-1": {"text": "Unknown", "color": "text", "index": 4},
}}]
COMPAT_MAP = [{"type": "value", "options": {
    "1": {"text": "Yes", "color": "green", "index": 0},
    "0": {"text": "No", "color": "red", "index": 1},
    "-1": {"text": "Unknown", "color": "text", "index": 2},
}}]
FINDING_MAP = [{"type": "value", "options": {k: {"text": v, "index": i} for i, (k, v) in enumerate({
    "end_of_life": "End of life",
    "incompatible_with_cluster": "Doesn't support current OCP",
    "blocks_cluster_upgrade": "Blocks upgrade to target OCP",
    "support_ending_soon": "Support ends soon",
    "update_with_cluster": "Update alongside OCP",
    "newer_version_available": "Newer version available",
    "unknown_version": "Version not in lifecycle data",
    "not_tracked": "No Red Hat lifecycle",
    "ok": "OK",
}.items())}}]
COLOR_CELL = {"type": "color-background", "mode": "basic"}

_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def target(expr, ref, legend="", table=True):
    t = {"datasource": DS, "expr": expr, "refId": ref, "legendFormat": legend}
    if table:
        t.update({"instant": True, "range": False, "format": "table"})
    else:
        t.update({"instant": True, "range": False})
    return t


def override(name, **props):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k.replace("__", "."), "value": v} for k, v in props.items()]}


def row(title, y, collapsed=False, panels=None):
    return {"id": nid(), "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": panels or []}


def stat(title, expr, x, y, w, color, description, unit="none"):
    return {
        "id": nid(), "type": "stat", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": 4},
        "targets": [target(expr, "A", table=False)],
        "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": color},
                                     "noValue": "0"}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background" if color != "text" else "value", "graphMode": "none",
                    "textMode": "value", "justifyMode": "center"},
    }


def table(title, description, x, y, w, h, targets, transformations, overrides, sort=None, defaults=None):
    return {
        "id": nid(), "type": "table", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets, "transformations": transformations,
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True, "cellOptions": {"type": "auto"}},
                                     **(defaults or {})},
                        "overrides": overrides},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False},
                    "sortBy": sort or []},
    }


def organize(exclude, rename, order=None):
    opts = {"excludeByName": {k: True for k in exclude}, "renameByName": rename}
    if order:
        opts["indexByName"] = {k: i for i, k in enumerate(order)}
    return {"id": "organize", "options": opts}


MERGE = {"id": "merge", "options": {}}
# Red Hat dates are midnight UTC and Grafana formats this unit in browser-local
# time, so every date query adds 12h: the calendar day then reads correctly
# from UTC-12 to UTC+11.
DATE = "time:YYYY-MM-DD"

panels = []
y = 0
panels.append(row("Fleet summary", y)); y += 1
panels += [
    stat("Clusters reporting", f"count(ocp_lifecycle_cluster_info{{{C}}})", 0, y, 4, "text",
         "Clusters with a running lifecycle exporter."),
    stat("Operators: action required", f"count(ocp_lifecycle_operator_status{{{C}}} == 2)", 4, y, 5, "red",
         "Installed operator versions that are end of life or do not support the cluster's current OpenShift version."),
    stat("Operators: plan an update", f"count(ocp_lifecycle_operator_status{{{C}}} == 1)", 9, y, 5, "orange",
         "Installed operator versions that block the update channel's target OpenShift version, or whose support ends soon."),
    stat("Operators: OK", f"count(ocp_lifecycle_operator_status{{{C}}} == 0)", 14, y, 5, "green",
         "Supported, and compatible with both the current and target OpenShift version."),
    stat("Not tracked by Red Hat", f"count(ocp_lifecycle_operator_status{{{C}}} == -1)", 19, y, 5, "text",
         "Community/certified operators with no Red Hat lifecycle entry, or a version the lifecycle data does not list."),
]
y += 4

cluster_targets = [
    target(f"max by (cluster, version, ocp_version, channel, target_ocp_version) (ocp_lifecycle_cluster_info{{{C}}})", "A"),
    target(f"max by (cluster, status) (ocp_lifecycle_cluster_status{{{C}}})", "B"),
    target(f"max by (cluster, phase) (ocp_lifecycle_cluster_support_phase{{{C}}})", "C"),
    target(f"(max by (cluster) (ocp_lifecycle_cluster_current_phase_end_timestamp_seconds{{{C}}}) + 43200) * 1000", "D"),
    target(f"max by (cluster) ((ocp_lifecycle_cluster_current_phase_end_timestamp_seconds{{{C}}} - time()) / 86400)", "E"),
    target(f"(max by (cluster) (ocp_lifecycle_cluster_support_end_timestamp_seconds{{{C}}}) + 43200) * 1000", "F"),
    target(f"(max by (cluster) (ocp_lifecycle_cluster_extended_support_end_timestamp_seconds{{{C}}}) + 43200) * 1000", "G"),
    target(f"count by (cluster) (ocp_lifecycle_operator_status{{{C}}} == 2)", "H"),
    target(f"count by (cluster) (ocp_lifecycle_operator_status{{{C}}} == 1)", "I"),
]
panels.append(table(
    "Clusters",
    "Each cluster's OpenShift version, the minor its update channel targets, and where that minor is in the Red Hat lifecycle.",
    0, y, 24, 6, cluster_targets,
    [MERGE, organize(
        ["Time", "Value #A", "status"],
        {"cluster": "Cluster", "version": "Version", "ocp_version": "Minor", "channel": "Channel",
         "target_ocp_version": "Channel target", "Value #B": "Status", "phase": "Phase",
         "Value #C": "Support level", "Value #D": "Phase ends", "Value #E": "Days left in phase",
         "Value #F": "Standard support ends", "Value #G": "Extended support ends",
         "Value #H": "Operators: action", "Value #I": "Operators: plan"},
        ["cluster", "version", "ocp_version", "channel", "target_ocp_version", "Value #B", "Value #C", "phase",
         "Value #D", "Value #E", "Value #F", "Value #G", "Value #H", "Value #I"])],
    [
        override("Status", mappings=STATUS_MAP, custom__cellOptions=COLOR_CELL, custom__width=130),
        override("Support level", mappings=PHASE_MAP, custom__cellOptions=COLOR_CELL, custom__width=140),
        override("Phase ends", unit=DATE),
        override("Standard support ends", unit=DATE),
        override("Extended support ends", unit=DATE),
        override("Days left in phase", unit="none", decimals=0),
        override("Operators: action", noValue="0",
                 thresholds={"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": "red", "value": 1}]},
                 custom__cellOptions={"type": "color-text"}),
        override("Operators: plan", noValue="0",
                 thresholds={"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": "orange", "value": 1}]},
                 custom__cellOptions={"type": "color-text"}),
    ],
    sort=[{"displayName": "Cluster", "desc": False}],
))
y += 6

panels.append(row("Installed operators", y)); y += 1
op_by = "cluster, operator_namespace, package"
op_targets = [
    target(f"max by ({op_by}, product, installed_version, lifecycle_version, channel, phase, compatible_ocp_versions, link) "
           f"(ocp_lifecycle_operator_info{{{C}}})", "A"),
    target(f"max by ({op_by}, status, reasons) (ocp_lifecycle_operator_status{{{C}}})", "B"),
    target(f"max by ({op_by}) (ocp_lifecycle_operator_support_phase{{{C}}})", "C"),
    target(f"max by ({op_by}) (ocp_lifecycle_operator_compatible_with_cluster{{{C}}})", "D"),
    target(f"max by ({op_by}) (ocp_lifecycle_operator_compatible_with_target{{{C}}})", "E"),
    target(f"(max by ({op_by}) (ocp_lifecycle_operator_support_end_timestamp_seconds{{{C}}}) + 43200) * 1000", "F"),
    target(f"max by ({op_by}, recommended_version) (ocp_lifecycle_operator_recommended_version_info{{{C}, for=\"current\"}})", "G"),
    # Renamed so it does not collide with G's recommended_version in the merge.
    target(f"max by ({op_by}, target_version) (label_replace("
           f"ocp_lifecycle_operator_recommended_version_info{{{C}, for=\"target\"}}, "
           f"\"target_version\", \"$1\", \"recommended_version\", \"(.*)\"))", "H"),
]
panels.append(table(
    "Operators",
    "Every OLM Subscription joined against the Red Hat lifecycle. Sorted worst first. "
    "\"For current OCP\" / \"For target OCP\" are the newest supported operator versions compatible with the cluster's "
    "current minor and with its update channel's target.",
    0, y, 24, 16, op_targets,
    [MERGE, organize(
        ["Time", "Value #A", "Value #G", "Value #H", "phase"],
        {"cluster": "Cluster", "package": "Package", "operator_namespace": "Namespace",
         "installed_version": "Installed", "lifecycle_version": "Lifecycle stream", "channel": "Channel",
         "Value #B": "Status", "status": "Finding", "reasons": "All findings", "phase": "Phase",
         "Value #C": "Support level", "Value #D": "Supports current OCP", "Value #E": "Supports target OCP",
         "compatible_ocp_versions": "Compatible OCP versions", "Value #F": "Support ends",
         "recommended_version": "For current OCP", "target_version": "For target OCP", "product": "Product"},
        ["cluster", "package", "operator_namespace", "installed_version", "lifecycle_version", "channel",
         "Value #B", "status", "Value #C", "phase", "Value #D", "Value #E", "compatible_ocp_versions", "Value #F",
         "recommended_version", "target_version", "product", "reasons", "link"]),
     {"id": "sortBy", "options": {"sort": [{"field": "Status", "desc": True}]}}],
    [
        override("Status", mappings=STATUS_MAP, custom__cellOptions=COLOR_CELL, custom__width=130),
        override("Finding", mappings=FINDING_MAP, custom__width=230),
        override("Package", custom__width=260),
        override("Support level", mappings=PHASE_MAP, custom__cellOptions=COLOR_CELL, custom__width=130),
        override("Supports current OCP", mappings=COMPAT_MAP, custom__cellOptions=COLOR_CELL, custom__width=110),
        override("Supports target OCP", mappings=COMPAT_MAP, custom__cellOptions=COLOR_CELL, custom__width=110),
        override("Support ends", unit=DATE, custom__width=110),
        override("link", custom__hidden=True),
        override("Product", links=[{"title": "Red Hat lifecycle policy", "url": "${__data.fields.link}",
                                    "targetBlank": True}]),
    ],
))
y += 16

panels.append({
    "id": nid(), "type": "bargauge", "title": "Operator support remaining (soonest first)",
    "description": "Days until standard support ends for each installed operator version. Negative means already past.",
    "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 12, "h": 10},
    "targets": [target(f"sort(bottomk(15, max by (cluster, package) ((ocp_lifecycle_operator_support_end_timestamp_seconds{{{C}}} - time()) / 86400)))",
                       "A", "{{package}} {{cluster}}", table=False)],
    "fieldConfig": {"defaults": {"unit": "suffix: days", "decimals": 0, "min": 0, "max": 365,
                                 "thresholds": {"mode": "absolute", "steps": [
                                     {"color": "red", "value": None}, {"color": "orange", "value": 30},
                                     {"color": "yellow", "value": 90}, {"color": "green", "value": 180}]},
                                 "color": {"mode": "thresholds"}}, "overrides": []},
    "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True, "valueMode": "text",
                "namePlacement": "left", "sizing": "auto",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
})
panels.append({
    "id": nid(), "type": "bargauge", "title": "OpenShift: days left in current phase",
    "description": "Days until each cluster's OpenShift minor leaves its current support phase.",
    "datasource": DS, "gridPos": {"x": 12, "y": y, "w": 12, "h": 10},
    "targets": [target(f"max by (cluster, ocp_version, phase) ((ocp_lifecycle_cluster_current_phase_end_timestamp_seconds{{{C}}} - time()) / 86400)",
                       "A", "{{cluster}} {{ocp_version}} – {{phase}}", table=False)],
    "fieldConfig": {"defaults": {"unit": "suffix: days", "decimals": 0, "min": 0, "max": 365,
                                 "thresholds": {"mode": "absolute", "steps": [
                                     {"color": "red", "value": None}, {"color": "orange", "value": 30},
                                     {"color": "yellow", "value": 90}, {"color": "green", "value": 180}]},
                                 "color": {"mode": "thresholds"}}, "overrides": []},
    "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True, "valueMode": "text",
                "namePlacement": "left", "sizing": "auto",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
})
y += 10

panels.append(row("OpenShift compatibility matrix", y)); y += 1
panels.append({
    **table(
        "Operator × OpenShift compatibility – $cluster",
        "Which OpenShift minors the installed version of each operator supports, per Red Hat. "
        "Compare against this cluster's current minor and its update channel's target (Clusters table above). "
        "Untracked operators are not listed.",
        0, y, 24, 14,
        [target(f"max by (package, lifecycle_version, ocp_version) (ocp_lifecycle_operator_ocp_compatibility{{{C}}})", "A")],
        [{"id": "groupingToMatrix", "options": {"columnField": "ocp_version", "rowField": "package",
                                                "valueField": "Value", "emptyValue": "null"}},
         # groupingToMatrix orders columns by first appearance; pin them.
         {"id": "organize", "options": {"renameByName": {"package\\ocp_version": "Operator"},
                                        "indexByName": {"package\\ocp_version": 0, **{
                                            f"{major}.{minor}": 1 + i for i, (major, minor) in enumerate(
                                                [(4, m) for m in range(1, 30)] + [(5, m) for m in range(0, 20)])}}}}],
        [override("Operator", custom__width=280, custom__align="left",
                  custom__cellOptions={"type": "auto"})],
        sort=[{"displayName": "Operator", "desc": False}],
        defaults={"mappings": [{"type": "value", "options": {"1": {"text": "✓", "color": "green", "index": 0}}},
                               {"type": "special", "options": {"match": "null", "result": {
                                   "text": "–", "color": "transparent", "index": 1}}}],
                  "custom": {"align": "center", "filterable": False, "cellOptions": COLOR_CELL},
                  "color": {"mode": "thresholds"},
                  "thresholds": {"mode": "absolute", "steps": [{"color": "transparent", "value": None}]}},
    ),
    "repeat": "cluster", "repeatDirection": "v",
})
y += 14

panels.append(row("OpenShift lifecycle", y)); y += 1
panels.append(table(
    "OpenShift minors: current phase",
    "Every supported OpenShift minor, plus any minor a cluster is running or targeting.",
    0, y, 8, 10,
    [target(f"max by (ocp_version, phase) (ocp_lifecycle_ocp_version_support_phase{{{C}}})", "A"),
     target(f"(max by (ocp_version) (ocp_lifecycle_ocp_version_support_end_timestamp_seconds{{{C}}}) + 43200) * 1000", "B")],
    [MERGE, organize(["Time"], {"ocp_version": "Minor", "phase": "Phase", "Value #A": "Support level",
                                "Value #B": "Standard support ends"},
                     ["ocp_version", "Value #A", "phase", "Value #B"])],
    [override("Support level", mappings=PHASE_MAP, custom__cellOptions=COLOR_CELL),
     override("Standard support ends", unit=DATE)],
    sort=[{"displayName": "Minor", "desc": True}],
))
panels.append(table(
    "OpenShift minors: phase end dates",
    "Published end date of each lifecycle phase. Empty where the phase does not apply (e.g. EUS on odd minors).",
    8, y, 16, 10,
    [target(f"(max by (ocp_version, phase) (ocp_lifecycle_ocp_version_phase_end_timestamp_seconds{{{C}}}) + 43200) * 1000", "A")],
    [{"id": "groupingToMatrix", "options": {"columnField": "phase", "rowField": "ocp_version",
                                            "valueField": "Value", "emptyValue": "null"}},
     {"id": "organize", "options": {
         "renameByName": {"ocp_version\\phase": "Minor"},
         "indexByName": {"ocp_version\\phase": 0, "Full support": 1, "Maintenance support": 2,
                         "Extended update support": 3, "Extended update support Term 2": 4,
                         "Extended update support Term 3": 5, "Extended life phase": 6}}}],
    [override("Minor", unit="string")],
    sort=[{"displayName": "Minor", "desc": True}],
    defaults={"unit": DATE, "noValue": "–"},
))
y += 10

panels.append(row("Exporter health", y, collapsed=True, panels=[
    {**stat("Lifecycle data age", f"max(time() - ocp_lifecycle_api_last_success_timestamp_seconds{{{C}}})", 0, y + 1, 8,
            "text", "Oldest successful Red Hat API fetch across the selected clusters. Refreshes every 4h.", unit="s")},
    {**stat("Cluster data age", f"max(time() - ocp_lifecycle_cluster_last_success_timestamp_seconds{{{C}}})", 8, y + 1, 8,
            "text", "Oldest successful read of ClusterVersion/Subscriptions. Refreshes every 10m.", unit="s")},
    {**stat("Clusters with stale data",
            f"count((time() - ocp_lifecycle_api_last_success_timestamp_seconds{{{C}}}) > 86400)", 16, y + 1, 8,
            "red", "Clusters whose exporter has not reached access.redhat.com in 24h.")},
]))



# Matches the label `cluster` in PromQL and legends, not the $cluster variable
# or metric names such as ocp_lifecycle_cluster_info.
_CLUSTER_LABEL = re.compile(r"(?<![\w$])cluster\b")


def _relabel(node, label):
    """Rewrites every reference to the `cluster` label as `label`."""
    if isinstance(node, list):
        return [_relabel(v, label) for v in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k in ("expr", "legendFormat", "definition") or (k == "query" and isinstance(v, str)):
            v = _CLUSTER_LABEL.sub(label, v)
        elif k in ("renameByName", "indexByName", "excludeByName"):
            v = {label if name == "cluster" else name: x for name, x in v.items()}
        out[k] = _relabel(v, label)
    return out


def dashboard(datasource=None, datasource_name=None, cluster_label="cluster"):
    """datasource: uid selected by default; None leaves Grafana's default data source.

    cluster_label: the label that tells clusters apart. `cluster` comes from
    ACM, Prometheus external labels or the collector config in this repo;
    OpenTelemetry's resource detection ships `k8s_cluster_name` instead.
    """
    current = {"text": datasource_name or datasource, "value": datasource} if datasource else {}
    otel = cluster_label != "cluster"
    d = {
        "uid": "ocp-lifecycle-otel" if otel else "ocp-lifecycle",
        "title": "OpenShift & operator lifecycle" + (" (OpenTelemetry)" if otel else ""),
        "description": "Red Hat product lifecycle for each cluster's OpenShift version and installed operators: "
                       "support phase, end dates, and OpenShift compatibility. Fed by ocp-lifecycle-exporter.",
        "tags": ["lifecycle", "openshift", "operators"],
        "timezone": "utc", "editable": True, "schemaVersion": 39, "version": 1,
        "refresh": "5m", "time": {"from": "now-6h", "to": "now"}, "graphTooltip": 0,
        "links": [{"title": "Red Hat product lifecycles", "type": "link", "targetBlank": True,
                   "url": "https://access.redhat.com/product-life-cycles"}],
        "templating": {"list": [
            {"name": "datasource", "label": "Data source", "type": "datasource", "query": "prometheus",
             "current": current, "hide": 0,
             "description": "Any Prometheus-compatible source holding the ocp_lifecycle_* series."},
            {"name": "cluster", "label": "Cluster", "type": "query", "datasource": DS,
             "query": {"query": "label_values(ocp_lifecycle_cluster_info, cluster)", "refId": "cluster"},
             "definition": "label_values(ocp_lifecycle_cluster_info, cluster)",
             "refresh": 2, "includeAll": True, "multi": True, "allValue": ".*",
             "current": {"text": ["All"], "value": ["$__all"]}, "sort": 1, "hide": 0},
        ]},
        "panels": panels,
    }
    return _relabel(d, cluster_label) if otel else d


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--datasource", help="uid of the data source selected by default")
    ap.add_argument("--cluster-label", default="cluster",
                    help="label that tells clusters apart (k8s_cluster_name for OpenTelemetry)")
    args = ap.parse_args()
    print(json.dumps(dashboard(args.datasource, cluster_label=args.cluster_label), indent=2, ensure_ascii=False))
