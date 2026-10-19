"""Twilio signature validation for the HTTP webhook and the WebSocket handshake."""
import os

from loguru import logger
from twilio.request_validator import RequestValidator

_validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
_SKIP = os.getenv("SKIP_TWILIO_SIGNATURE", "false").lower() == "true"

if _SKIP:
    logger.warning("SKIP_TWILIO_SIGNATURE=true: request signatures are NOT being checked")


def _should_skip() -> bool:
    return os.getenv("SKIP_TWILIO_SIGNATURE", "false").lower() in ("true", "1", "yes")


def is_valid_webhook(url: str, form_params: dict, signature: str) -> bool:
    """Validate a Twilio webhook. `url` must be the exact public URL Twilio called."""
    if _should_skip():
        logger.debug("Signature check skipped (SKIP_TWILIO_SIGNATURE=true)")
        return True
    if not signature:
        logger.warning(f"Webhook rejected: missing X-Twilio-Signature header for {url}")
        return False
    ok = _validator.validate(url, form_params, signature)
    if not ok:
        logger.warning(
            f"Webhook rejected: bad signature for {url}. "
            "Set SKIP_TWILIO_SIGNATURE=true in .env for local testing if URL mismatches."
        )
    return ok


def is_valid_websocket(candidate_urls: list[str], signature: str) -> bool:
    """Validate the signature Twilio sends on the Media Streams handshake.

    The WebSocket has no form params. Twilio's docs should be checked for the exact URL
    form that is signed (wss:// vs https://), so we accept either and log which matched.
    """
    if _should_skip():
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
