import json
import logging
from typing import Any

from read_api.service import get_slot_by_id
from shared.waitlist import promote_waitlist
from shared.metrics import emit_metric

logger = logging.getLogger(__name__)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        try:
            envelope = json.loads(record.get("body") or "{}")
            detail = envelope.get("detail") or {}
            slot = get_slot_by_id(str(detail["slot_id"]))
            if slot:
                offer = promote_waitlist(slot, seat_already_held=bool(detail.get("seat_held")))
                if offer:
                    emit_metric("WaitlistPromotions")
                    logger.info("Created waitlist offer %s.", offer["offer_id"])
        except Exception:
            logger.exception("Waitlist promotion failed.")
            failures.append({"itemIdentifier": record.get("messageId", "")})
    return {"batchItemFailures": failures}
