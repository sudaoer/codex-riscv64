#!/usr/bin/env python3
"""Compatibility entry point for native K3 validation."""

from __future__ import annotations

import sys

from validate import checked_remote_directory, github_run, main, parser, run

__all__ = ["checked_remote_directory", "github_run", "main", "parser", "run"]


if __name__ == "__main__":
    if not any(
        argument == "--target" or argument.startswith("--target=")
        for argument in sys.argv[1:]
    ):
        sys.argv[1:1] = ["--target", "k3"]
    raise SystemExit(main())
