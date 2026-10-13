import io
import unittest
import urllib.error

from ocp_lifecycle_exporter.kube import KubeClient, _csv_name_prefix, _version_from_csv_name


class FakeKube(KubeClient):
    def __init__(self, responses):
        super().__init__(api_url="https://example.invalid", token="t")
        self.responses = responses
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        value = self.responses[path]
        if isinstance(value, int):
            raise urllib.error.HTTPError(path, value, "error", {}, io.BytesIO())
        return value


CSV_BASE = "/apis/operators.coreos.com/v1alpha1/namespaces/openshift-operators/clusterserviceversions"


def csv(name, version, phase="Succeeded"):
    return {"metadata": {"name": name}, "spec": {"version": version}, "status": {"phase": phase}}


class KubeTest(unittest.TestCase):
    def test_version_from_csv_name(self):
        self.assertEqual(_version_from_csv_name("aap-operator.v2.6.0-0.1787258175"), "2.6.0-0.1787258175")
        self.assertEqual(_version_from_csv_name("costmanagement-metrics-operator.4.5.0"), "4.5.0")
        self.assertEqual(_version_from_csv_name("prometheusoperator.0.56.3"), "0.56.3")
        self.assertIsNone(_version_from_csv_name("weird"))

    def test_csv_name_prefix(self):
        self.assertEqual(_csv_name_prefix("kiali-operator.v2.22.1"), "kiali-operator")

    def test_cluster_state_uses_completed_history_during_upgrade(self):
        k = FakeKube({"/apis/config.openshift.io/v1/clusterversions/version": {
            "spec": {"channel": "eus-4.22"},
            "status": {"desired": {"version": "4.21.3"},
                       "history": [{"version": "4.21.3", "state": "Partial"},
                                   {"version": "4.20.29", "state": "Completed"}]},
        }})
        s = k.cluster_state()
        self.assertEqual((s.version, s.desired_version, s.target_minor), ("4.20.29", "4.21.3", "4.22"))
        self.assertEqual(k.cluster_state("4.21").target_minor, "4.21")

    def test_stale_installed_csv_resolves_to_successor(self):
        k = FakeKube({
            "/apis/operators.coreos.com/v1alpha1/subscriptions": {"items": [{
                "metadata": {"namespace": "openshift-operators", "name": "kiali-ossm"},
                "spec": {"name": "kiali-ossm", "channel": "stable", "source": "redhat-operators"},
                "status": {"installedCSV": "kiali-operator.v2.22.1"},
            }]},
            f"{CSV_BASE}/kiali-operator.v2.22.1": 404,
            CSV_BASE: {"items": [csv("kiali-operator.v2.22.2", "2.22.2"),
                                 csv("servicemeshoperator3.v3.3.2", "3.3.2"),
                                 csv("kiali-operator.v2.22.3", "2.22.3", phase="Installing")]},
        })
        [o] = k.installed_operators()
        self.assertEqual((o.csv, o.version), ("kiali-operator.v2.22.2", "2.22.2"))

    def test_subscription_without_csv(self):
        k = FakeKube({"/apis/operators.coreos.com/v1alpha1/subscriptions": {"items": [{
            "metadata": {"namespace": "x", "name": "pending"}, "spec": {"name": "foo"}, "status": {},
        }]}})
        [o] = k.installed_operators()
        self.assertEqual((o.csv, o.version), ("", None))


if __name__ == "__main__":
    unittest.main()
