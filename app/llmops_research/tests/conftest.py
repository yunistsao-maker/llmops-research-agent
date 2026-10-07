"""Test configuration: make the app importable and give tests a clean, offline environment."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# nothing in the unit tests may depend on the wall clock or on real AWS credentials
os.environ.setdefault("RESEARCH_NOW", "2026-10-02T03:00:00Z")
os.environ.setdefault("RESEARCH_TODAY", "2026-10-02")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="run tests that fetch real public web pages")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs outbound internet access to public sites (run with --live)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    import pytest

    skip = pytest.mark.skip(reason="needs --live (real public web access)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
