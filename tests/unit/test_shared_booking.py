import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("AWS_REGION", "ap-south-1")
sys.path.insert(0, "src")

from shared.booking import calculate_refund, slot_sk_for


def test_zero_refund_tier():
    policy = {"tiers": [{"minimum_hours": 0, "refund_percent": 0, "inclusive": True, "name": "zero"}], "doctor_refund_percent": 100, "no_show_refund_percent": 0}
    result = calculate_refund(500, datetime(2026, 12, 1, 10, tzinfo=timezone.utc), datetime(2026, 12, 1, 9, tzinfo=timezone.utc), cancellation_by="patient", policy=policy)
    assert result["refund_amount"] == 0


def test_slot_sk_derivation():
    start = datetime(2026, 12, 1, 10, 30, tzinfo=timezone.utc)
    assert slot_sk_for("ct-1", start) == f"{int(start.timestamp())}#ct-1"
