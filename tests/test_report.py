import unittest

from ocp_lifecycle_exporter.config import DEFAULT_PACKAGE_OVERRIDES
from ocp_lifecycle_exporter.kube import ClusterState, InstalledOperator
from ocp_lifecycle_exporter.metrics import render_report
from ocp_lifecycle_exporter.report import (
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_UNKNOWN,
    SEVERITY_WARNING,
    build_report,
)

from .test_lifecycle import NOW, load_catalog

OCP = "Red Hat OpenShift Container Platform"
CLUSTER = ClusterState(version="4.20.29", desired_version="4.20.29", channel="eus-4.22", target_minor="4.22")


def op(package, version, namespace="ns"):
    return InstalledOperator(namespace=namespace, subscription=package, package=package, channel="stable",
                             source="redhat-operators", csv=f"{package}.v{version}", version=version)


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()

    def build(self, operators, cluster=CLUSTER, warn_days=90):
        return build_report(self.catalog, cluster, operators, NOW, OCP, DEFAULT_PACKAGE_OVERRIDES, warn_days)

    def one(self, package, version, **kw):
        return self.build([op(package, version)], **kw).operators[0]

    def test_cluster(self):
        c = self.build([]).cluster
        self.assertEqual(c.minor, "4.20")
        self.assertEqual(c.lifecycle.phase_name, "Maintenance support")
        self.assertEqual((c.status, c.severity), ("ok", SEVERITY_OK))

    def test_cluster_warns_before_end_of_support(self):
        c = self.build([], warn_days=800).cluster
        self.assertEqual((c.status, c.severity), ("support_ending_soon", SEVERITY_WARNING))

    def test_ocp_versions_include_supported_and_own(self):
        old = ClusterState(version="4.10.5", desired_version="4.10.5", channel="stable-4.10", target_minor="4.10")
        r = self.build([], cluster=old)
        names = [o.version for o in r.ocp_versions]
        self.assertIn("4.22", names)
        self.assertIn("4.10", names)  # EOL but this cluster runs it
        self.assertNotIn("4.9", names)
        self.assertEqual((r.cluster.status, r.cluster.severity), ("end_of_life", SEVERITY_CRITICAL))

    def test_operator_blocking_upgrade(self):
        o = self.one("advanced-cluster-management", "2.15.8")
        self.assertEqual(o.version.name, "2.15")
        self.assertEqual(o.compatible_now, 1)
        self.assertEqual(o.compatible_target, 0)
        self.assertEqual((o.status, o.severity), ("blocks_cluster_upgrade", SEVERITY_WARNING))
        self.assertEqual(o.recommended_target.name, "2.17")

    def test_platform_aligned_operator_is_informational(self):
        o = self.one("lvms-operator", "4.20.2")
        self.assertEqual(o.compatible_target, 0)
        self.assertEqual((o.status, o.severity), ("update_with_cluster", SEVERITY_OK))

    def test_incompatible_with_cluster_is_critical(self):
        o = self.one("redhat-oadp-operator", "1.4.9")
        self.assertEqual(o.compatible_now, 0)
        self.assertEqual((o.status, o.severity), ("incompatible_with_cluster", SEVERITY_CRITICAL))
        self.assertIn("newer_version_available", o.reasons)

    def test_eol_operator_is_critical(self):
        o = self.one("advanced-cluster-management", "2.10.1")
        self.assertEqual(o.reasons[0], "end_of_life")
        self.assertEqual(o.severity, SEVERITY_CRITICAL)

    def test_not_tracked_and_unknown_version(self):
        r = self.build([op("grafana-operator", "5.24.0"), op("advanced-cluster-management", "9.9.9")])
        self.assertEqual((r.operators[0].status, r.operators[0].severity), ("not_tracked", SEVERITY_UNKNOWN))
        self.assertEqual((r.operators[1].status, r.operators[1].severity), ("unknown_version", SEVERITY_UNKNOWN))

    def test_no_target_change_does_not_flag_upgrade(self):
        same = ClusterState(version="4.20.29", desired_version="4.20.29", channel="stable-4.20", target_minor="4.20")
        o = self.build([op("advanced-cluster-management", "2.15.8")], cluster=same).operators[0]
        self.assertNotIn("blocks_cluster_upgrade", o.reasons)
        self.assertEqual(o.severity, SEVERITY_OK)


class MetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog = load_catalog()
        report = build_report(catalog, CLUSTER, [op("advanced-cluster-management", "2.15.8", "open-cluster-management"),
                                                 op("grafana-operator", "5.24.0")],
                              NOW, OCP, DEFAULT_PACKAGE_OVERRIDES, 90)
        cls.text = render_report(report, {"api_failures": 0, "cluster_failures": 0, "api_products": 10})

    def test_every_sample_has_type(self):
        declared = {line.split()[2] for line in self.text.splitlines() if line.startswith("# TYPE")}
        for line in self.text.splitlines():
            if line and not line.startswith("#"):
                self.assertIn(line.split("{")[0].split(" ")[0], declared)

    def test_cluster_info(self):
        self.assertIn('ocp_lifecycle_cluster_info{ocp_version="4.20",version="4.20.29",desired_version="4.20.29",'
                      'channel="eus-4.22",target_ocp_version="4.22"} 1', self.text)

    def test_operator_series(self):
        self.assertIn('ocp_lifecycle_operator_status{operator_namespace="open-cluster-management",'
                      'package="advanced-cluster-management",status="blocks_cluster_upgrade",'
                      'reasons="blocks_cluster_upgrade,newer_version_available"} 1', self.text)
        self.assertIn('ocp_lifecycle_operator_ocp_compatibility{operator_namespace="open-cluster-management",'
                      'package="advanced-cluster-management",lifecycle_version="2.15",ocp_version="4.21"} 1', self.text)
        self.assertIn('ocp_lifecycle_operator_recommended_version_info{operator_namespace="open-cluster-management",'
                      'package="advanced-cluster-management",for="target",ocp_version="4.22",recommended_version="2.17"} 1',
                      self.text)
        self.assertIn('ocp_lifecycle_operator_compatible_with_target{operator_namespace="open-cluster-management",'
                      'package="advanced-cluster-management",target_ocp_version="4.22"} 0', self.text)

    def test_untracked_operator_has_info_and_status_only(self):
        lines = [l for l in self.text.splitlines() if 'package="grafana-operator"' in l]
        self.assertEqual({l.split("{")[0] for l in lines},
                         {"ocp_lifecycle_operator_info", "ocp_lifecycle_operator_status"})

    def test_render_without_report(self):
        text = render_report(None, {"api_failures": 2, "cluster_failures": 0})
        self.assertIn("ocp_lifecycle_api_fetch_failures_total 2", text)
        self.assertNotIn("ocp_lifecycle_cluster_info", text)


if __name__ == "__main__":
    unittest.main()
