"""Update notices follow Python release ordering, including experimental versions."""

import pytest

from wizolt.base import WizoltError
from wizolt.ui.cli.update import UpdateChecker, UpdateStatus


@pytest.mark.parametrize(("current", "latest", "newer"), [
    ("0.73.0a1", "0.73.0a2", True),
    ("0.73.0a1", "0.73.0", True),
    ("0.73.0rc1", "0.73.0", True),
    ("0.73.0", "0.73.0a1", False),
    ("0.73.0a1", "0.72.0", False),
    ("0.73.0a1", "0.73.0a1", False),
    ("0.73.0", "0.73.1", True),
    ("0.73.0", "0.74.0 garbage", False),
    ("invalid", "0.74.0", False),
])
def test_update_version_order(current, latest, newer):
    assert UpdateStatus(latest=latest).newer_than(current) is newer


def test_pypi_version_must_be_a_complete_valid_version():
    assert UpdateChecker.parse_latest(b'{"info":{"version":"0.73.0a1"}}') == "0.73.0a1"
    with pytest.raises(WizoltError, match="invalid PyPI"):
        UpdateChecker.parse_latest(b'{"info":{"version":"0.73.0 garbage"}}')
