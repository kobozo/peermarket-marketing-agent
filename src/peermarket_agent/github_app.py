"""GitHub App authentication — mints short-lived installation tokens."""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog
from jwt.api_jws import PyJWS

log = structlog.get_logger(__name__)

_jws = PyJWS()


@dataclass
class _CachedToken:
    token: str
    expires_at: datetime


class GitHubAppClient:
    """Mint and cache GitHub App installation tokens.

    JWT is minted per call (cheap). Installation token is cached until
    ~60s before its real expiry.
    """

    _GH_API = "https://api.github.com"

    def __init__(self, app_id: int, private_key: str, installation_id: int) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._cached: _CachedToken | None = None

    def __repr__(self) -> str:
        return (
            f"GitHubAppClient(app_id={self._app_id}, "
            f"installation_id={self._installation_id}, "
            f"private_key=***REDACTED***)"
        )

    def _mint_app_jwt(self) -> str:
        now = int(time.time())
        # Use lower-level PyJWS so we can keep `iss` as an integer (GitHub
        # accepts it; PyJWT's high-level `encode` enforces a string-only
        # check on `iss`).
        payload = json.dumps({"iat": now - 60, "exp": now + 540, "iss": self._app_id}).encode()
        return _jws.encode(payload, self._private_key, algorithm="RS256")

    async def get_installation_token(self) -> str:
        now = datetime.now(UTC)
        if self._cached and self._cached.expires_at.timestamp() - now.timestamp() > 60:
            return self._cached.token
        app_jwt = self._mint_app_jwt()
        url = f"{self._GH_API}/app/installations/{self._installation_id}/access_tokens"
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(
                url,
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                },
            )
            resp.raise_for_status()
            body = resp.json()
        self._cached = _CachedToken(
            token=body["token"],
            expires_at=datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")),
        )
        log.info("github_app.token.minted", expires_at=body["expires_at"])
        return self._cached.token
