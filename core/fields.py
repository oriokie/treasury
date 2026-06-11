"""Application-layer encryption for sensitive settings (API keys, SMS credentials).

Values are encrypted with Fernet (AES-128-CBC + HMAC) before being written to the
database and decrypted on load, so plaintext credentials never sit in the table or
in database backups. The key is derived from ``TREASURY_ENCRYPTION_KEY`` if set,
otherwise from Django's ``SECRET_KEY`` (so it works out of the box in development).

Decryption is tolerant: a value that fails to decrypt is returned unchanged. This
makes migrating an existing database with legacy plaintext values safe — old values
remain readable and are re-encrypted the next time they are saved.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

_PREFIX = "enc1:"  # marks a value we wrote, so we never try to decrypt plaintext


def _fernet():
    material = os.environ.get("TREASURY_ENCRYPTION_KEY") or settings.SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    if value in (None, ""):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value: str) -> str:
    if value in (None, ""):
        return value
    if not value.startswith(_PREFIX):
        return value  # legacy plaintext — return as-is
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return value


class EncryptedCharField(models.CharField):
    """A CharField whose value is stored encrypted at rest."""

    description = "Encrypted character field"

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def to_python(self, value):
        if value is None:
            return value
        return decrypt(value) if isinstance(value, str) and value.startswith(_PREFIX) else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt(value)
