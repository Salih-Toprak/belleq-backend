"""Fernet encryption for agent BYOK provider keys.

Mirrors the master's credential_crypto: the same ``CREDENTIAL_ENCRYPTION_KEY``
(a Fernet key) is used, and ciphertext carries a ``fernet:`` prefix so a value
can be recognised as encrypted. When the key is empty (dev only) the value is
stored as plaintext — never do this in production.

Only the agent's BYOK provider key passes through here. The ciphertext is stored
in ``agents.api_key_encrypted`` and is NEVER returned by an API serializer or
written to a log line; ``decrypt_secret`` is called only when handing the key to
the executor at run time.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger(__name__)

_FERNET_PREFIX = "fernet:"


def _fernet(key: str):
    from cryptography.fernet import Fernet

    return Fernet(key.strip().encode("utf-8"))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage in ``api_key_encrypted``.

    Empty input yields empty output (no key stored). With no encryption key
    configured the value is returned verbatim (dev only).
    """
    value = (plaintext or "").strip()
    if not value:
        return ""
    key = (settings.CREDENTIAL_ENCRYPTION_KEY or "").strip()
    if not key:
        logger.warning("agent_key_stored_plaintext set CREDENTIAL_ENCRYPTION_KEY in production")
        return value
    token = _fernet(key).encrypt(value.encode("utf-8")).decode("utf-8")
    return _FERNET_PREFIX + token


def decrypt_secret(stored: str) -> str:
    """Decrypt a stored ``api_key_encrypted`` value back to plaintext.

    Returns "" on empty/undecryptable input. Never logs the secret or the error
    detail (which could leak ciphertext) beyond a generic warning.
    """
    value = (stored or "").strip()
    if not value:
        return ""
    if not value.startswith(_FERNET_PREFIX):
        # No key configured at write time (dev) — value is plaintext.
        return value
    key = (settings.CREDENTIAL_ENCRYPTION_KEY or "").strip()
    if not key:
        logger.warning("agent_key_decrypt_no_key")
        return ""
    try:
        token = value[len(_FERNET_PREFIX):].encode("utf-8")
        return _fernet(key).decrypt(token).decode("utf-8")
    except Exception:  # noqa: BLE001 — never surface ciphertext/key in the message
        logger.warning("agent_key_decrypt_failed")
        return ""
