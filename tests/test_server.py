import unittest

from ocp_lifecycle_exporter.config import Config
from ocp_lifecycle_exporter.kube import ClusterState, InstalledOperator
from ocp_lifecycle_exporter.server import Exporter


class FlakyKube:
    def __init__(self):
        self.version = "4.20.29"
        self.fail_operators = False

    def cluster_state(self, target=None):
        return ClusterState(self.version, self.version, "stable-4.20", "4.20")

    def installed_operators(self):
        if self.fail_operators:
            raise TimeoutError("timed out")
        return [InstalledOperator("ns", "acm", "advanced-cluster-management", "release-2.15",
                                  "redhat-operators", "advanced-cluster-management.v2.15.8", "2.15.8")]


class ExporterTest(unittest.TestCase):
    def test_partial_cluster_read_keeps_last_snapshot(self):
        kube = FlakyKube()
        e = Exporter(Config(), kube)
        e.refresh_cluster()
        kube.version, kube.fail_operators = "4.21.1", True
        e.refresh_cluster()
        self.assertEqual(e.cluster.version, "4.20.29")
        self.assertEqual(len(e.operators), 1)
        self.assertEqual(e.health["cluster_failures"], 1)


if __name__ == "__main__":
    unittest.main()
