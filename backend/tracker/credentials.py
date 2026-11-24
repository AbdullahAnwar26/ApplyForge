"""
Encryption of stored third-party credentials (SMTP app passwords).

Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from ``CREDENTIALS_KEY``
or, when that is unset, from ``SECRET_KEY``. Changing that key makes stored
passwords unreadable; the user then enters the password again (reported as a
clear error, never a crash). Plain text is never logged or returned by the API.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class CredentialError(Exception):
    pass


def _fernet():
    secret = getattr(settings, "CREDENTIALS_KEY", "") or settings.SECRET_KEY
    digest = hashlib.sha256(("applyforge-credentials:" + secret).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value):
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token):
    if not token:
        raise CredentialError("No password is saved. Enter the password again.")
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise CredentialError(
            "The saved password can no longer be read (the server key changed). Enter it again."
        ) from exc
