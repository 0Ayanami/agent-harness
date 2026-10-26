"""Prepare the parent of pytest's configurable temporary directory."""

from pathlib import Path


def pytest_configure(config):
    # pytest creates basetemp itself, but its parent may be absent after clean.
    if config.option.basetemp:
        Path(config.option.basetemp).resolve().parent.mkdir(parents=True, exist_ok=True)
