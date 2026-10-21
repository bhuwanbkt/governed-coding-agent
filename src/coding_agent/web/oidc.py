"""OpenID Connect login (Authorization Code flow with PKCE), for any standard provider.

Built for ZITADEL, but nothing here is ZITADEL-specific except two defaults: the project
audience scope and the project roles claim. Okta, Microsoft Entra ID or Keycloak work by
changing configuration (issuer, client, roles claim).

What is verified before a user is trusted:
- `state` (ties the callback to the login this browser started; prevents login CSRF)
- PKCE `code_verifier` (a stolen authorization code is useless without it)
- the ID token's signature against the provider's published keys (JWKS), its issuer, audience,
  expiry, and `nonce` (prevents replaying an old token)

Tokens from the provider are used once to establish identity and roles, then discarded.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

_NAME = re.compile(r"[^A-Za-z0-9._@+-]")


class OIDCError(RuntimeError):
    """Login failed. The message is safe to show to the user."""


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    project_id: str | None = None
    roles_claim: str | None = None

    @property
    def scopes(self) -> str:
        scopes = ["openid", "profile", "email"]
        if self.project_id:  # ZITADEL: token audience must include the project to carry its roles
            scopes.append(f"urn:zitadel:iam:org:project:id:{self.project_id}:aud")
        return " ".join(scopes)

    def role_claims(self) -> list[str]:
        claims = [self.roles_claim] if self.roles_claim else []
        if self.project_id:
            claims.append(f"urn:zitadel:iam:org:project:{self.project_id}:roles")
        claims.append("urn:zitadel:iam:org:project:roles")
        return claims


@dataclass(frozen=True)
class LoginRequest:
    """What we remember (in the session) between redirecting to the provider and the callback."""

    state: str
    nonce: str
    code_verifier: str
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "nonce": self.nonce,
            "verifier": self.code_verifier,
            "at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoginRequest:
        return cls(
            state=data["state"], nonce=data["nonce"], code_verifier=data["verifier"], started_at=data["at"]
        )


@dataclass(frozen=True)
class Identity:
    subject: str
    name: str  # what the audit log records
    email: str | None
    roles: frozenset[str]


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_login_request() -> LoginRequest:
    return LoginRequest(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=secrets.token_urlsafe(64),  # 86 chars, within RFC 7636's 43-128
    )


def roles_from_claims(claims: dict[str, Any], claim_names: list[str]) -> frozenset[str]:
    """Roles from the first claim present. ZITADEL uses {role: {orgId: orgDomain}}; others a list."""
    for name in claim_names:
        value = claims.get(name)
        if isinstance(value, dict):
            return frozenset(str(k) for k in value)
        if isinstance(value, list):
            return frozenset(str(v) for v in value)
        if isinstance(value, str):
            return frozenset(value.replace(",", " ").split())
    return frozenset()


def display_name(claims: dict[str, Any]) -> str:
    raw = str(claims.get("preferred_username") or claims.get("email") or claims["sub"])
    return _NAME.sub("_", raw)[:64]


class OIDCClient:
    def __init__(self, config: OIDCConfig, http: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._http = http
        self._metadata: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15)
        return self._http

    async def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
            try:
                response = await self._client().get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise OIDCError("The identity provider is unreachable.") from exc
            data = response.json()
            if data.get("issuer", "").rstrip("/") != self.config.issuer.rstrip("/"):
                raise OIDCError("The identity provider's issuer does not match OIDC_ISSUER.")
            self._metadata = data
        return self._metadata

    async def authorization_url(self, redirect_uri: str, login: LoginRequest) -> str:
        meta = await self.metadata()
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.config.scopes,
            "state": login.state,
            "nonce": login.nonce,
            "code_challenge": pkce_challenge(login.code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    async def finish(self, *, code: str, state: str, redirect_uri: str, login: LoginRequest) -> Identity:
        if not secrets.compare_digest(state, login.state):
            raise OIDCError("This login link doesn't match the one you started. Please sign in again.")
        if time.time() - login.started_at > 600:
            raise OIDCError("The sign-in took too long. Please try again.")
        meta = await self.metadata()
        try:
            response = await self._client().post(
                meta["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": login.code_verifier,
                },
                auth=(self.config.client_id, self.config.client_secret),
            )
        except httpx.HTTPError as exc:
            raise OIDCError("The identity provider is unreachable.") from exc
        if response.status_code != 200:
            raise OIDCError("The identity provider rejected the sign-in. Please try again.")
        tokens = response.json()
        claims = await self.verify_id_token(str(tokens.get("id_token", "")), nonce=login.nonce)
        roles = roles_from_claims(claims, self.config.role_claims())
        if not roles and tokens.get("access_token") and meta.get("userinfo_endpoint"):
            roles = await self._roles_from_userinfo(
                meta["userinfo_endpoint"], str(tokens["access_token"]), claims
            )
        return Identity(
            subject=str(claims["sub"]),
            name=display_name(claims),
            email=claims.get("email"),
            roles=roles,
        )

    async def _roles_from_userinfo(
        self, url: str, access_token: str, claims: dict[str, Any]
    ) -> frozenset[str]:
        """Some providers only put roles in userinfo. The `sub` must match the verified ID token."""
        try:
            response = await self._client().get(url, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError:
            return frozenset()
        if response.status_code != 200:
            return frozenset()
        info = response.json()
        if info.get("sub") != claims.get("sub"):
            return frozenset()
        return roles_from_claims(info, self.config.role_claims())

    async def _signing_key(self, kid: str | None) -> Any:
        meta = await self.metadata()
        for attempt in range(2):  # refetch once: the provider may have rotated its keys
            if self._jwks is None or attempt == 1:
                try:
                    response = await self._client().get(meta["jwks_uri"])
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise OIDCError("The identity provider's keys are unavailable.") from exc
                self._jwks = response.json()
            for key in jwt.PyJWKSet.from_dict(self._jwks).keys:
                if kid is None or key.key_id == kid:
                    return key.key
        raise OIDCError("The sign-in token was signed with an unknown key.")

    async def verify_id_token(self, token: str, *, nonce: str) -> dict[str, Any]:
        if not token:
            raise OIDCError("The identity provider returned no ID token.")
        try:
            header = jwt.get_unverified_header(token)
            key = await self._signing_key(header.get("kid"))
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256", "PS256"],
                audience=self.config.client_id,
                issuer=(await self.metadata())["issuer"],  # exact form the provider uses
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
                leeway=60,
            )
        except jwt.PyJWTError as exc:
            raise OIDCError("The sign-in token could not be verified.") from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OIDCError("The sign-in token was not issued for this login.")
        return claims
