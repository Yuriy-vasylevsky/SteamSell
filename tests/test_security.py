import base64
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.admin import AdminOnly, parse_field
from app.monobank import verify_signature
from app.security import Vault


def test_webhook_signature():
    key = ec.generate_private_key(ec.SECP256R1())
    public = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode()
    body = b'{"status":"success"}'
    signature = base64.b64encode(key.sign(body, ec.ECDSA(hashes.SHA256()))).decode()
    assert verify_signature(body, signature, public)
    assert not verify_signature(body + b" ", signature, public)
    assert not verify_signature(body, "invalid", public)


def test_vault_confidentiality_and_authentication():
    vault = Vault(Fernet.generate_key().decode())
    token = vault.encrypt("password123")
    assert "password123" not in token
    assert vault.decrypt(token) == "password123"
    with pytest.raises(InvalidToken):
        Vault(Fernet.generate_key().decode()).decrypt(token)


async def test_admin_filter_is_server_side(shop):
    assert await AdminOnly()(SimpleNamespace(from_user=SimpleNamespace(id=99)), shop)
    assert not await AdminOnly()(SimpleNamespace(from_user=SimpleNamespace(id=1)), shop)


@pytest.mark.parametrize("text", ["0", "-1", "NaN", "Infinity", "1.001", "1000001", "abc"])
def test_invalid_prices(text):
    with pytest.raises(ValueError):
        parse_field("price", SimpleNamespace(text=text), None)


def test_decimal_price():
    assert parse_field("price", SimpleNamespace(text="599,50"), None) == 59950
