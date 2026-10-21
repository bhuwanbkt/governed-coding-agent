"""OIDC client: discovery, PKCE, token exchange and ID token verification against a fake provider.

The provider is an `httpx.MockTransport` with a freshly generated RSA key, so the tests exercise
real JWT signatures without any network."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from coding_agent.web.oidc import (
    LoginRequest,
    OIDCClient,
    OIDCConfig,
    OIDCError,
    display_name,
    new_login_request,
    pkce_challenge,
    roles_from_claims,
)

ISSUER = "https://idp.example.test"
CLIENT_ID = "client-123"
CLIENT_SECRET = "s" * 40  # fake, low entropy
PROJECT_ID = "proj-1"
REDIRECT = "https://app.example.test/auth/callback"
ROLES_CLAIM = f"urn:zitadel:iam:org:project:{PROJECT_ID}:roles"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**data, "kid": kid, "use": "sig", "alg": "RS256"}


def id_token(nonce: str, *, key: rsa.RSAPrivateKey = KEY, kid: str = "k1", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": [CLIENT_ID, PROJECT_ID],
        "sub": "user-1",
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
        "preferred_username": "bhuwan",
        ROLES_CLAIM: {"submitter": {"org1": "example.test"}},
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class FakeProvider:
    def __init__(self) -> None:
        self.token_response: dict[str, Any] = {}
        self.token_status = 200
        self.userinfo: dict[str, Any] = {}
        self.token_requests: list[httpx.Request] = []
        self.issuer = ISSUER

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{ISSUER}/oauth/v2/authorize",
                    "token_endpoint": f"{ISSUER}/oauth/v2/token",
                    "userinfo_endpoint": f"{ISSUER}/oidc/v1/userinfo",
                    "jwks_uri": f"{ISSUER}/oauth/v2/keys",
                },
            )
        if path == "/oauth/v2/keys":
            return httpx.Response(200, json={"keys": [jwk(KEY, "k1")]})
        if path == "/oauth/v2/token":
            self.token_requests.append(request)
            return httpx.Response(self.token_status, json=self.token_response)
        if path == "/oidc/v1/userinfo":
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(404)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


def make_client(provider: FakeProvider) -> OIDCClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(provider.handle))
    return OIDCClient(OIDCConfig(ISSUER, CLIENT_ID, CLIENT_SECRET, project_id=PROJECT_ID), http)


def finish(client: OIDCClient, login: LoginRequest, *, state: str | None = None) -> Any:
    return asyncio.run(
        client.finish(code="the-code", state=state or login.state, redirect_uri=REDIRECT, login=login)
    )


# --------------------------------------------------------------------------- helpers
def test_pkce_challenge_is_s256_of_the_verifier() -> None:
    login = new_login_request()
    digest = hashlib.sha256(login.code_verifier.encode()).digest()
    assert pkce_challenge(login.code_verifier) == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert 43 <= len(login.code_verifier) <= 128


def test_login_request_round_trips_through_the_session() -> None:
    login = new_login_request()
    assert LoginRequest.from_dict(json.loads(json.dumps(login.as_dict()))) == login


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"approver": {"o": "d"}, "submitter": {"o": "d"}}, {"approver", "submitter"}),
        (["approver"], {"approver"}),
        ("submitter, approver", {"submitter", "approver"}),
        (None, set()),
    ],
)
def test_roles_from_claims_accepts_zitadel_and_list_shapes(value: Any, expected: set[str]) -> None:
    assert roles_from_claims({"roles": value}, ["roles"]) == expected


def test_display_name_is_sanitised() -> None:
    assert display_name({"sub": "1", "preferred_username": "<script>x</script>"}) == "_script_x__script_"
    assert display_name({"sub": "abc"}) == "abc"


def test_zitadel_scopes_request_the_project_audience() -> None:
    config = OIDCConfig(ISSUER, CLIENT_ID, CLIENT_SECRET, project_id=PROJECT_ID)
    assert f"urn:zitadel:iam:org:project:id:{PROJECT_ID}:aud" in config.scopes
    assert config.role_claims()[0] == ROLES_CLAIM


# --------------------------------------------------------------------------- flow
def test_authorization_url_uses_pkce_state_and_nonce(provider: FakeProvider) -> None:
    login = new_login_request()
    url = asyncio.run(make_client(provider).authorization_url(REDIRECT, login))
    query = parse_qs(urlparse(url).query)

    assert url.startswith(f"{ISSUER}/oauth/v2/authorize?")
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["state"] == [login.state]
    assert query["nonce"] == [login.nonce]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [pkce_challenge(login.code_verifier)]
    assert login.code_verifier not in url


def test_successful_sign_in_returns_identity_with_roles(provider: FakeProvider) -> None:
    login = new_login_request()
    provider.token_response = {"id_token": id_token(login.nonce), "access_token": "at"}
    identity = finish(make_client(provider), login)

    assert identity.subject == "user-1"
    assert identity.name == "bhuwan"
    assert identity.roles == {"submitter"}
    sent = parse_qs(provider.token_requests[0].content.decode())
    assert sent["code_verifier"] == [login.code_verifier]
    assert provider.token_requests[0].headers["authorization"].startswith("Basic ")


def test_roles_fall_back_to_userinfo_with_matching_subject(provider: FakeProvider) -> None:
    login = new_login_request()
    provider.token_response = {"id_token": id_token(login.nonce, **{ROLES_CLAIM: None}), "access_token": "at"}
    provider.userinfo = {"sub": "user-1", ROLES_CLAIM: {"approver": {"o": "d"}}}
    assert finish(make_client(provider), login).roles == {"approver"}

    login = new_login_request()
    provider.token_response = {"id_token": id_token(login.nonce, **{ROLES_CLAIM: None}), "access_token": "at"}
    provider.userinfo = {"sub": "someone-else", ROLES_CLAIM: {"approver": {"o": "d"}}}
    assert finish(make_client(provider), login).roles == frozenset()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"nonce": "replayed"}, "not issued for this login"),
        ({"aud": "another-client"}, "could not be verified"),
        ({"iss": "https://evil.example.test"}, "could not be verified"),
        ({"exp": int(time.time()) - 3600}, "could not be verified"),
    ],
)
def test_bad_id_tokens_are_rejected(provider: FakeProvider, overrides: dict[str, Any], message: str) -> None:
    login = new_login_request()
    nonce = overrides.pop("nonce", login.nonce)
    provider.token_response = {"id_token": id_token(nonce, **overrides)}
    with pytest.raises(OIDCError, match=message):
        finish(make_client(provider), login)


def test_token_signed_by_an_unknown_key_is_rejected(provider: FakeProvider) -> None:
    login = new_login_request()
    provider.token_response = {"id_token": id_token(login.nonce, key=OTHER_KEY, kid="k1")}
    with pytest.raises(OIDCError, match="could not be verified"):
        finish(make_client(provider), login)

    provider.token_response = {"id_token": id_token(login.nonce, key=OTHER_KEY, kid="k9")}
    with pytest.raises(OIDCError, match="unknown key"):
        finish(make_client(provider), login)


def test_state_mismatch_and_stale_logins_are_refused_before_the_token_call(provider: FakeProvider) -> None:
    login = new_login_request()
    with pytest.raises(OIDCError, match="doesn't match"):
        finish(make_client(provider), login, state="forged")

    stale = LoginRequest(login.state, login.nonce, login.code_verifier, time.time() - 3600)
    with pytest.raises(OIDCError, match="too long"):
        finish(make_client(provider), stale)
    assert provider.token_requests == []


def test_rejected_code_and_issuer_mismatch(provider: FakeProvider) -> None:
    login = new_login_request()
    provider.token_status = 400
    with pytest.raises(OIDCError, match="rejected"):
        finish(make_client(provider), login)

    provider.issuer = "https://somewhere-else.example.test"
    with pytest.raises(OIDCError, match="issuer does not match"):
        asyncio.run(make_client(provider).metadata())
