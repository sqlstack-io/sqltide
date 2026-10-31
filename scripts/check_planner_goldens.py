"""Check that planner outputs for the golden corpus are unchanged."""

import argparse

from scripts.planner_goldens.main.check_planner_goldens import check_planner_goldens


def _parse_args() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="Rewrite every golden")
    return parser.parse_args()


def main() -> int:
    return check_planner_goldens(update=_parse_args().update)


if __name__ == "__main__":
    raise SystemExit(main())
