# ocp-lifecycle-dashboard

Tells you, per cluster, whether your OpenShift version and every installed
operator are still supported, and whether the operators support the OpenShift
version your update channel is heading to.

```
 each managed cluster                                   hub (sno)
┌──────────────────────────────────────┐        ┌────────────────────────────────┐
│ ocp-lifecycle-exporter               │        │ ACM observability (Thanos)     │
│   ├─ Red Hat Product Life Cycle API  │        │   ▲ cluster="<name>" added     │
│   │    every 4h (access.redhat.com)  │        │   │                            │
│   ├─ ClusterVersion, Subscriptions,  │        │ grafana-lab                    │
│   │    CSVs every 10m                │        │   "OpenShift & operator        │
│   └─ /metrics  ocp_lifecycle_*       │        │    lifecycle" dashboard        │
│          ▲                           │        └────────────────────────────────┘
│ platform Prometheus ── ACM metrics-collector (allowlist) ─────────▲
│ PrometheusRule ── alerts ── ACM hub Alertmanager                  │
└──────────────────────────────────────┘
```

The data comes from the same API that backs
<https://access.redhat.com/product-life-cycles>. Every operator there carries its
OLM package name, which is what the exporter joins against each Subscription.

## What it answers

| Question | Where |
|---|---|
| Is my OpenShift minor in full, maintenance or EUS support, and until when? | Clusters table, *OpenShift lifecycle* row |
| Is each installed operator version still supported? | Operators table → *Support level*, *Support ends* |
| Does it support the OpenShift I'm on? | *Supports current OCP* |
| Does it support the OpenShift my channel targets (`eus-4.22` → 4.22)? | *Supports target OCP* |
| Which operator version should I move to? | *For current OCP* / *For target OCP* |
| Which OpenShift minors does each operator support? | Compatibility matrix |

Each operator gets one headline **status**:

| Value | Status | Meaning |
|---|---|---|
| 2 | `end_of_life` | Installed version is past end of support |
| 2 | `incompatible_with_cluster` | Doesn't list the cluster's current minor |
| 1 | `blocks_cluster_upgrade` | Doesn't list the channel's target minor; update the operator first |
| 1 | `support_ending_soon` | Support ends within `WARN_DAYS` (90) |
| 0 | `update_with_cluster` | Versioned in lockstep with OCP (LVMS, MetalLB, CNV…); update it at each hop |
| 0 | `newer_version_available` | Fine, but a newer supported version exists |
| 0 | `ok` | |
| -1 | `unknown_version` | Product is tracked but the installed version isn't listed |
| -1 | `not_tracked` | No Red Hat lifecycle (community/certified operators) |

## Metrics

All gauges, prefixed `ocp_lifecycle_`. Operator series are keyed by
`operator_namespace` + `package`; ACM adds `cluster` on the hub.

| Metric | Labels | Value |
|---|---|---|
| `cluster_info` | `version`, `ocp_version`, `channel`, `target_ocp_version` | 1 |
| `cluster_status` | `ocp_version`, `status` | severity |
| `cluster_support_phase` | `ocp_version`, `phase` | 3 full, 2 maint, 1 extended, 0 EOL |
| `cluster_{current_phase,support,extended_support}_end_timestamp_seconds` | `ocp_version` | unix time |
| `ocp_version_support_phase` | `ocp_version`, `phase` | phase code |
| `ocp_version_phase_end_timestamp_seconds` | `ocp_version`, `phase` | unix time |
| `ocp_version_{support,extended_support}_end_timestamp_seconds` | `ocp_version` | unix time |
| `operator_info` | `product`, `installed_version`, `lifecycle_version`, `channel`, `phase`, `compatible_ocp_versions`, `tracked`, `link`, … | 1 |
| `operator_status` | `status`, `reasons` | severity |
| `operator_support_phase` | `lifecycle_version`, `phase` | phase code |
| `operator_{current_phase,support,extended_support}_end_timestamp_seconds` | | unix time |
| `operator_compatible_with_cluster` / `_with_target` | `ocp_version` / `target_ocp_version` | 1, 0, -1 unknown |
| `operator_ocp_compatibility` | `lifecycle_version`, `ocp_version` | 1 per supported minor |
| `operator_recommended_version_info` | `for` (current/target), `ocp_version`, `recommended_version` | 1 |
| `api_last_success_timestamp_seconds`, `api_fetch_failures_total`, `cluster_last_success_timestamp_seconds`, `cluster_read_failures_total` | | exporter health |

Roughly 500 series per cluster.

## Configuration

| Env | Default | |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `14400` | Red Hat API refresh |
| `RETRY_INTERVAL_SECONDS` | `300` | Retry after a failed fetch |
| `CLUSTER_REFRESH_SECONDS` | `600` | ClusterVersion/Subscription refresh |
| `WARN_DAYS` | `90` | `support_ending_soon` window |
| `TARGET_OCP_VERSION` | from channel | Plan against a different minor |
| `PACKAGE_PRODUCT_OVERRIDES` | | `pkg=Product Name;pkg2=Other` for packages the API doesn't list |
| `LIFECYCLE_API_URL` | Red Hat API | |
| `LISTEN_PORT` | `9120` | |

Built-in overrides cover `lightspeed-operator`, `servicemeshoperator3` and
`devworkspace-operator`, which the API lists without (or under another) package.

## Deploying

Manifests live in [ocp-homelab](https://github.com/shane-snyder/ocp-homelab):

- `components/ocp-lifecycle-exporter/`: namespace (platform-monitored),
  read-only RBAC, Deployment, ServiceMonitor, PrometheusRule
- `clusters/{sno,sno-mini}/values.yaml`: the Argo CD app on each cluster
- `components/acm-observability/05-…-allowlist.yaml`: forwards the series to the hub
- `components/grafana-lab/041-…` + `043-…`: ACM fleet datasource and the dashboard
- `components/ocp-lifecycle-exporter-pipeline/` (sno only): builds the image

The image lives on the homelab Quay at
`quay-server.apps.sno.shanehomelab.com/ocp-lifecycle/ocp-lifecycle-exporter`,
public for pulls like x-followers, so no cluster needs a pull secret. A
CronJob on sno polls this repo every 5 minutes. When a commit touches
`ocp_lifecycle_exporter/`, `tests/` or the `Containerfile`, it starts a Tekton
run: clone → unit tests → buildah push `:<sha>` → tag `:latest` → restart the
exporter on sno. Other clusters pick up `:latest` the next time their pod
restarts. GitHub Actions only runs the tests.

One-time setup in Quay and Vault:

1. In Quay, create the org `ocp-lifecycle` and the repo `ocp-lifecycle-exporter`,
   then make the repo **public**.
2. Create a robot with write access to it and download its Docker
   `config.json`.
3. Store that JSON in Vault at `ocp-lifecycle/pipeline-quay-push`, property
   `config.json`.

Until the first build lands, the exporter pods sit in `ImagePullBackOff`;
they recover on their own once `:latest` exists.

## Development

Standard library only, no dependencies.

```sh
python3 -m unittest discover -s tests -t .

# Run once against whatever cluster `oc` is logged into
KUBE_API_URL=$(oc whoami --show-server) KUBE_TOKEN=$(oc whoami -t) \
  python3 -m ocp_lifecycle_exporter --once

# Regenerate the dashboard CR after editing hack/gen_dashboard.py
python3 hack/gen_dashboard.py --cr \
  > ../ocp-homelab/components/grafana-lab/043-grafana-dashboard-ocp-lifecycle.yaml
```

## Caveats

- The output is only as good as Red Hat's data. For example, cluster
  observability 1.5 lists 4.19 and 4.21 but not 4.20, so it shows as
  incompatible on 4.20 clusters.
- Some operators use a different version scheme than their lifecycle entry
  (Tempo's CSV is 0.x, its lifecycle stream is 3.x). These show as
  `unknown_version`.
- Dashboard dates add 12h before display: Red Hat publishes midnight-UTC dates
  and Grafana formats them in browser-local time.
