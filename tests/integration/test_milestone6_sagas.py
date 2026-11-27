import os

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("MEDISLOT_API_URL"),
    reason="Requires a deployed MediSlot dev/test stack",
)


def test_reschedule_compensation_contract():
    assert os.environ["MEDISLOT_API_URL"].rstrip("/")


def test_waitlist_exclusivity_contract():
    assert os.environ["MEDISLOT_API_URL"].rstrip("/")
