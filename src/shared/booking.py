"""Shared booking refund rules and calculations."""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from shared.config import get_ssm_parameter


def round_money(amount: Any) -> Decimal:
    """Return a currency amount rounded to two decimal places."""
    return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_money(amount: Any) -> str:
    """Format a rounded currency amount without a currency-specific symbol."""
    return f"{round_money(amount):.2f}"


def load_refund_policy() -> dict[str, Any]:
    parameter_name = os.environ.get("REFUND_POLICY_PARAMETER_NAME", "").strip()
    if not parameter_name:
        raise RuntimeError("REFUND_POLICY_PARAMETER_NAME is not configured.")
    try:
        policy = json.loads(get_ssm_parameter(parameter_name, decrypt=False))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Refund policy in SSM must be valid JSON.") from exc
    if not isinstance(policy, dict) or not isinstance(policy.get("tiers"), list):
        raise RuntimeError("Refund policy in SSM must contain a tiers list.")
    if not policy["tiers"]:
        raise RuntimeError("Refund policy in SSM must contain at least one tier.")
    minimum_hours_seen: set[Decimal] = set()
    for tier in policy["tiers"]:
        if not isinstance(tier, dict):
            raise RuntimeError("Every refund policy tier must be an object.")
        if "minimum_hours" not in tier or "refund_percent" not in tier:
            raise RuntimeError("Refund tiers require minimum_hours and refund_percent.")
        try:
            minimum_hours = Decimal(str(tier["minimum_hours"]))
            refund_percent = Decimal(str(tier["refund_percent"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise RuntimeError("Refund tier thresholds and percentages must be numeric.") from exc
        if not minimum_hours.is_finite() or minimum_hours < 0:
            raise RuntimeError("Refund tier thresholds must be finite and non-negative.")
        if minimum_hours in minimum_hours_seen:
            raise RuntimeError("Refund tier thresholds must be unique.")
        minimum_hours_seen.add(minimum_hours)
        if (
            not refund_percent.is_finite()
            or refund_percent != refund_percent.to_integral_value()
            or not 0 <= refund_percent <= 100
        ):
            raise RuntimeError("Refund percentages must be between 0 and 100.")
        if "inclusive" in tier and not isinstance(tier["inclusive"], bool):
            raise RuntimeError("Refund tier inclusive flags must be booleans.")
    if Decimal("0") not in minimum_hours_seen:
        raise RuntimeError("Refund policy must include a zero-hour tier.")
    zero_hour_tier = next(
        tier for tier in policy["tiers"]
        if Decimal(str(tier["minimum_hours"])) == 0
    )
    if not zero_hour_tier.get("inclusive", False):
        raise RuntimeError("The zero-hour refund tier must include its lower boundary.")
    return policy


def calculate_refund(
    amount: Any,
    appointment_start: datetime,
    cancelled_at: datetime,
    *,
    cancellation_by: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Calculate the ledger refund using the ordered SSM policy tiers."""
    amount_decimal = round_money(amount)
    if amount_decimal < 0:
        raise ValueError("Refund amount cannot be negative.")
    start = _as_utc(appointment_start)
    cancelled = _as_utc(cancelled_at)
    initiator = cancellation_by.strip().lower()

    if initiator == "doctor":
        refund_percent = int(policy.get("doctor_refund_percent", 100))
        tier_name = "doctor_cancellation"
    elif initiator == "no_show":
        refund_percent = int(policy.get("no_show_refund_percent", 0))
        tier_name = "no_show"
    elif initiator == "patient":
        hours_until_appointment = Decimal(str((start - cancelled).total_seconds())) / Decimal(3600)
        tier = _matching_tier(hours_until_appointment, policy["tiers"])
        refund_percent = int(tier["refund_percent"])
        tier_name = str(tier.get("name") or "patient_cancellation")
    else:
        raise ValueError("cancellation_by must be patient, doctor, or no_show.")

    if not 0 <= refund_percent <= 100:
        raise RuntimeError("Refund percentages must be between 0 and 100.")
    refund_amount = round_money(amount_decimal * Decimal(refund_percent) / Decimal(100))
    return {
        "refund_amount": refund_amount,
        "refund_percent": refund_percent,
        "tier": tier_name,
    }


def _matching_tier(hours_until_appointment: Decimal, tiers: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply strict upper cutoffs: exactly 48h, 24h, and 6h use the next tier."""
    ordered = sorted(
        tiers,
        key=lambda item: Decimal(str(item["minimum_hours"])),
        reverse=True,
    )
    for tier in ordered:
        minimum_hours = Decimal(str(tier["minimum_hours"]))
        inclusive = bool(tier.get("inclusive", False))
        if hours_until_appointment >= minimum_hours if inclusive else hours_until_appointment > minimum_hours:
            return tier
    raise RuntimeError("Refund policy does not cover the cancellation time.")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
