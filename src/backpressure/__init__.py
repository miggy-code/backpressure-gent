"""Backpressure: a verification gate for agent-written pull requests."""

import sys

from backpressure.cli import main as _main


def main() -> None:
    sys.exit(_main())
