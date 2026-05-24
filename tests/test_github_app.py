"""GitHub App auth tests — JWT minting + install token exchange."""
import time
from unittest.mock import AsyncMock

import jwt
import pytest

from peermarket_agent.github_app import GitHubAppClient


def _generate_test_pem() -> str:
    """Generate a throwaway RSA-2048 PEM for signing in tests.

    The PEM is regenerated per session because real RSA signing requires a
    parseable key — a fixed dummy PEM cannot be loaded by `cryptography`.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


_TEST_PEM = _generate_test_pem()


@pytest.fixture
def client():
    return GitHubAppClient(
        app_id=12345,
        private_key=_TEST_PEM,
        installation_id=67890,
    )


def test_mint_app_jwt_signs_with_RS256(client):
    token = client._mint_app_jwt()
    headers = jwt.get_unverified_header(token)
    assert headers["alg"] == "RS256"


def test_mint_app_jwt_includes_iss_and_expiry(client):
    token = client._mint_app_jwt()
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["iss"] == 12345
    assert payload["exp"] > int(time.time())
    assert payload["exp"] - payload["iat"] <= 600  # GH max 10 min


async def test_get_installation_token_calls_correct_endpoint(client, monkeypatch):
    fake_post = AsyncMock()
    fake_post.return_value.status_code = 201
    fake_post.return_value.json = lambda: {
        "token": "ghs_FAKETOKEN",
        "expires_at": "2026-05-23T11:00:00Z",
    }
    fake_post.return_value.raise_for_status = lambda: None

    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    tok = await client.get_installation_token()
    assert tok == "ghs_FAKETOKEN"
    args, kwargs = fake_post.call_args
    assert args[0].endswith("/app/installations/67890/access_tokens")


async def test_get_installation_token_caches_until_expiry(client, monkeypatch):
    fake_post = AsyncMock()
    fake_post.return_value.status_code = 201
    fake_post.return_value.raise_for_status = lambda: None
    # 1 hour in the future
    fake_post.return_value.json = lambda: {
        "token": "ghs_FAKETOKEN",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await client.get_installation_token()
    await client.get_installation_token()
    assert fake_post.call_count == 1  # cached
