"""
Record the expected output of the pre-auto_cpu strategies into tests/golden/.

Run only on code whose legacy strategy output is known good:

    python3 -m tests.make_golden
"""
import os

from tests.legacy_fixture import SCENARIOS, GOLDEN_DIR, golden_path, dump


def main():
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    for name, scenario in SCENARIOS.items():
        with open(golden_path(name), 'w') as f:
            f.write(dump(scenario()) + '\n')
        print(f"wrote {golden_path(name)}")


if __name__ == "__main__":
    main()
