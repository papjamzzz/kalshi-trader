"""
Kalshi RSA Auth — shared signing module for KK + KK Trader.
Kalshi now requires RSA-PSS signed headers on every request.

Required env vars:
  KALSHI_KEY_ID          — the UUID Key ID from kalshi.com/account/profile
  KALSHI_PRIVATE_KEY_PATH — path to the .key file downloaded when you created the key
                            default: ~/kalshi.key
"""

import os
import time
import base64
from datetime import datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend


def _load_key():
    key_path = os.getenv(
        "KALSHI_PRIVATE_KEY_PATH",
        os.path.expanduser("~/kalshi.key")
    )
    if not os.path.exists(key_path):
        raise FileNotFoundError(
            f"Kalshi private key not found at: {key_path}\n"
            f"Set KALSHI_PRIVATE_KEY_PATH in .env or place key at ~/kalshi.key"
        )
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )


def _sign(private_key, text: str) -> str:
    message = text.encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def signed_headers(method: str, path: str) -> dict:
    """
    Returns the three Kalshi auth headers for a given request.

    method : HTTP verb, e.g. "GET", "POST"
    path   : URL path including leading slash, e.g. "/trade-api/v2/markets"
             Query params are automatically stripped before signing.
    """
    key_id = os.getenv("KALSHI_KEY_ID", "")
    if not key_id:
        raise ValueError("KALSHI_KEY_ID not set in .env")

    # Strip query params from path before signing (Kalshi requirement)
    clean_path = path.split("?")[0]

    ts_ms = str(int(time.time() * 1000))
    msg   = ts_ms + method.upper() + clean_path

    private_key = _load_key()
    sig = _sign(private_key, msg)

    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type":            "application/json",
    }
