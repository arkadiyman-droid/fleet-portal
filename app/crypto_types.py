"""
app/crypto_types.py — Encrypted-at-rest column type.

Controller.fleet_api_token is a live credential (the shared secret a site's
EMS instance trusts for read-only status polling — see EMS's own
use_controller/fleet/token.py), so it's Fernet-encrypted before it ever
reaches Postgres. Decryption happens transparently on read.

Requires FLEET_TOKEN_ENCRYPTION_KEY (a Fernet.generate_key() value) in the
environment — see config.py / .env.example.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


@lru_cache(maxsize=4)
def _fernet_for_key(key: bytes) -> Fernet:
    return Fernet(key)


def _fernet() -> Fernet:
    key = current_app.config["FLEET_TOKEN_ENCRYPTION_KEY"]
    return _fernet_for_key(key.encode() if isinstance(key, str) else key)


class EncryptedString(TypeDecorator):
    """Transparently Fernet-encrypts on write, decrypts on read. Backed by
    Text, not a guessed-length VARCHAR — Fernet ciphertext (base64, ~1.4x
    plaintext + ~57 bytes fixed overhead) has no small natural upper bound
    worth pre-computing."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return _fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken:
            # Wrong/rotated key or corrupt data — surface as "no token" so a
            # key rotation degrades gracefully instead of crashing the page.
            return None
