"""Direct wrapper for the shape surface check."""

from __future__ import annotations

from scripts.shape_surface.main.check import run_shape_surface_check


def main(argv: list[str] | None = None) -> int:
    """Check the public shape surface against the recorded 0.65.x baseline."""
    return run_shape_surface_check(argv)


if __name__ == "__main__":
    raise SystemExit(main())
