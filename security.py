"""Twilio signature validation for the HTTP webhook and the WebSocket handshake."""
import os

from loguru import logger
from twilio.request_validator import RequestValidator

_validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
_SKIP = os.getenv("SKIP_TWILIO_SIGNATURE", "false").lower() == "true"

if _SKIP:
    logger.warning("SKIP_TWILIO_SIGNATURE=true: request signatures are NOT being checked")


def is_valid_webhook(url: str, form_params: dict, signature: str) -> bool:
    """Validate a Twilio webhook. `url` must be the exact public URL Twilio called."""
    if _SKIP:
        return True
    if not signature:
        logger.warning("Webhook rejected: missing X-Twilio-Signature")
        return False
    ok = _validator.validate(url, form_params, signature)
    if not ok:
        logger.warning("Webhook rejected: bad signature (check PUBLIC_BASE_URL matches Twilio config)")
    return ok


def is_valid_websocket(candidate_urls: list[str], signature: str) -> bool:
    """Validate the signature Twilio sends on the Media Streams handshake.

    The WebSocket has no form params. Twilio's docs should be checked for the exact URL
    form that is signed (wss:// vs https://), so we accept either and log which matched.
    """
    if _SKIP:
        return True
    if not signature:
        logger.warning("WebSocket rejected: missing X-Twilio-Signature")
        return False
    for url in candidate_urls:
        if _validator.validate(url, {}, signature):
            logger.debug(f"WebSocket signature matched URL form: {url}")
            return True
    logger.warning(f"WebSocket rejected: bad signature (tried {candidate_urls})")
    return False
