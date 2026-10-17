"""ФИО между двумя репликами: шифрование, срок жизни и изоляция звонков."""

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken


class IdentityDrafts:
    def __init__(self, key: bytes, *, ttl: int = 300) -> None:
        self.cipher = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(b"identity-draft\0" + key).digest())
        )
        self.ttl = ttl

    def seal(self, call_id: str, last: str, first: str, middle: str | None) -> str:
        payload = json.dumps([call_id, last, first, middle], ensure_ascii=False).encode()
        return self.cipher.encrypt(payload).decode("ascii")

    def open(self, call_id: str, token: str) -> dict[str, str | None] | None:
        try:
            owner, last, first, middle = json.loads(self.cipher.decrypt(token, ttl=self.ttl))
        except (InvalidToken, ValueError, TypeError):
            return None
        if owner != call_id:
            return None
        return {"last_name": last, "first_name": first, "middle_name": middle}
