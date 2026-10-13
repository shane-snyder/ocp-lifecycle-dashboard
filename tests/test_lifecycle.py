import json
import os
import unittest
from datetime import datetime, timezone

from ocp_lifecycle_exporter.lifecycle import (
    PHASE_EOL,
    PHASE_EXTENDED,
    PHASE_FULL,
    PHASE_MAINTENANCE,
    iter_ocp_versions,
    parse_catalog,
    parse_ocp_list,
    parse_version,
    phase_category,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "products.json")
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def load_catalog():
    with open(FIXTURE) as f:
        return parse_catalog(json.load(f))


def utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


class ParsingTest(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(parse_version("4.20.0-202610061650"), (4, 20, 0))
        self.assertEqual(parse_version("v2.6.0+0.1787258175"), (2, 6, 0))
        self.assertEqual(parse_version("7.x"), (7,))
        self.assertEqual(parse_version("5.8 (For logging)"), (5, 8))
        self.assertIsNone(parse_version("N/A"))
        self.assertIsNone(parse_version(None))

    def test_parse_ocp_list(self):
        self.assertEqual(parse_ocp_list("4.19, 4.20,4.21"), ["4.19", "4.20", "4.21"])
        self.assertEqual(parse_ocp_list("4.22, 5.0"), ["4.22", "5.0"])
        self.assertEqual(parse_ocp_list("N/A"), [])
        self.assertEqual(parse_ocp_list(None), [])

    def test_phase_category(self):
        self.assertEqual(phase_category("Full support"), PHASE_FULL)
        self.assertEqual(phase_category("Maintenance Support 1"), PHASE_MAINTENANCE)
        self.assertEqual(phase_category("Extended update support Term 2"), PHASE_EXTENDED)
        self.assertEqual(phase_category("Extended life cycle support (ELS) add-on"), PHASE_EXTENDED)
        self.assertIsNone(phase_category("General availability"))
        self.assertIsNone(phase_category("End of Life"))


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()
        cls.ocp = cls.catalog.product("Red Hat OpenShift Container Platform")

    def ocp_version(self, name):
        return next(v for v in self.ocp.versions if v.name == name)

    def test_ocp_versions_skip_ocp3_and_eus_rows(self):
        names = [v.name for v in iter_ocp_versions(self.ocp)]
        self.assertIn("4.20", names)
        self.assertNotIn("3", names)
        self.assertNotIn("4.6 EUS", names)

    def test_ocp_420_in_maintenance(self):
        v = self.ocp_version("4.20")
        code, window = v.current_phase(NOW)
        self.assertEqual(code, PHASE_MAINTENANCE)
        self.assertEqual(window.name, "Maintenance support")
        self.assertEqual(v.support_end(), utc(2027, 4, 21))
        self.assertEqual(v.extended_support_end(), utc(2028, 10, 21))

    def test_last_day_of_phase_is_inclusive(self):
        v = self.ocp_version("4.21")  # full support ends 2026-09-09
        self.assertEqual(v.current_phase(datetime(2026, 9, 9, 18, tzinfo=timezone.utc))[0], PHASE_FULL)
        self.assertEqual(v.current_phase(datetime(2026, 9, 10, 1, tzinfo=timezone.utc))[0], PHASE_MAINTENANCE)

    def test_old_version_is_eol(self):
        self.assertEqual(self.ocp_version("4.10").current_phase(NOW)[0], PHASE_EOL)

    def test_package_lookup_handles_comma_separated_packages(self):
        self.assertEqual(self.catalog.by_package["mcg-operator"].name, "Red Hat OpenShift Data Foundation")
        self.assertEqual(self.catalog.by_package["odf-operator"].name, "Red Hat OpenShift Data Foundation")

    def test_override_maps_package_without_lifecycle_package(self):
        p = self.catalog.product_for_package("lightspeed-operator", {"lightspeed-operator": "Red Hat OpenShift Lightspeed"})
        self.assertEqual(p.name, "Red Hat OpenShift Lightspeed")
        self.assertIsNone(self.catalog.product_for_package("lightspeed-operator", {}))

    def test_match_version_prefers_exact_prefix(self):
        fio = self.catalog.by_package["file-integrity-operator"]
        self.assertEqual(fio.match_version("1.5.0").name, "1.5.0")

    def test_match_version_falls_back_to_major_minor(self):
        cma = self.catalog.by_package["openshift-custom-metrics-autoscaler-operator"]
        self.assertEqual(cma.match_version("2.19.0-4").name, "2.19.1")

    def test_match_version_wildcard_major(self):
        amq = self.catalog.by_package["amq-broker-rhel9"]
        self.assertEqual(amq.match_version("7.12.3-opr-1").name, "7.x")

    def test_match_version_does_not_confuse_1_1_with_1_10(self):
        cert = self.catalog.by_package["compliance-operator"]
        self.assertEqual(cert.match_version("1.10.0").name, "1.10")
        self.assertIsNone(cert.match_version("1.1.0"))

    def test_match_version_unknown(self):
        acm = self.catalog.by_package["advanced-cluster-management"]
        self.assertIsNone(acm.match_version("9.9.9"))
        self.assertIsNone(acm.match_version(None))

    def test_newest_supported_for(self):
        acm = self.catalog.by_package["advanced-cluster-management"]
        self.assertEqual(acm.newest_supported_for("4.22", NOW).name, "2.17")
        self.assertIsNone(acm.newest_supported_for("4.1", NOW))


if __name__ == "__main__":
    unittest.main()
