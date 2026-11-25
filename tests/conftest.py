import importlib
import sys

import pytest

# Make sure Home Assistant's loader finds this repo's custom_components package
# (not the test harness's own testing_config/custom_components).
import custom_components.mission_control  # noqa: F401,E402

pytest_plugins = "pytest_homeassistant_custom_component"

# The branch set the store-level tests run against (an install can configure any set).
TEST_BRANCHES = [
    {"key": "home", "name": "Home", "work": False, "assistant": False},
    {"key": "projects", "name": "Projects", "work": False, "assistant": True},
    {"key": "personal", "name": "Personal", "work": False, "assistant": False},
    {"key": "work", "name": "Work", "work": True, "assistant": False},
    {"key": "automotive", "name": "Automotive", "work": False, "assistant": False},
    {"key": "lab", "name": "Lab", "work": False, "assistant": True},
    {"key": "studio", "name": "Studio", "work": False, "assistant": True},
]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def branch_registry():
    """Load TEST_BRANCHES into whichever copy of the package a test imported."""
    for pkg in ("mission_control", "custom_components.mission_control"):
        if pkg in sys.modules:
            importlib.import_module(f"{pkg}.const").apply_branches(TEST_BRANCHES)
    yield
