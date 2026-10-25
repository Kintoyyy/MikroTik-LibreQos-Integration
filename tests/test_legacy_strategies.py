"""The cpu / flat / ap_only / ap_site / full strategies must keep their exact output."""
import unittest

from tests.legacy_fixture import SCENARIOS, golden_path, dump


class LegacyStrategyGoldenTest(unittest.TestCase):
    def test_scenarios_match_golden(self):
        for name, scenario in SCENARIOS.items():
            with self.subTest(name):
                with open(golden_path(name)) as f:
                    expected = f.read().strip()
                self.assertEqual(dump(scenario()), expected)


if __name__ == "__main__":
    unittest.main()
