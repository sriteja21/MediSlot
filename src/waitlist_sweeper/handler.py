from typing import Any

from shared.waitlist import expire_waitlist_offers
from shared.metrics import emit_metric


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, int]:
    processed = expire_waitlist_offers()
    if processed:
        emit_metric("OffersExpired", processed)
    return {"expired_offers_processed": processed}
