"""Черновик ФИО нельзя прочитать без ключа, перенести в другой звонок или использовать позднее."""

from app.dialogue.identity import IdentityDrafts


def test_encrypted_draft_is_bound_to_call_key_and_ttl():
    drafts = IdentityDrafts(b"stable-private-key" * 3)
    token = drafts.seal("call-1", "Тестов", "Александр", "Примерович")
    assert "Тестов" not in token
    assert drafts.open("call-1", token)["first_name"] == "Александр"
    assert drafts.open("call-2", token) is None
    assert IdentityDrafts(b"another-private-key" * 3).open("call-1", token) is None
    old = drafts.cipher.encrypt_at_time(b'["call-1","last","first",null]', 1).decode()
    assert drafts.open("call-1", old) is None
    assert drafts.open("call-1", token[:-3] + "bad") is None
