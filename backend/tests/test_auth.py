import base64
import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from conftest import _events, _fields
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from savetube.config import Settings
from savetube.web.app import create_app
from savetube.web.auth import _safe_next

ISSUER = "https://issuer.test"
CLIENT_ID = "1234"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"})


def _id_token(nonce: str) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "u1",
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
        "name": "Kim",
    }
    return jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY)


class FakeIssuer:
    """Answers the outbound httpx2 calls Authlib makes. TestClient's own transport is untouched."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[httpx2.Request] = []

    def respond(self, method: str, path: str, json: object) -> None:
        self.routes[(method, f"{ISSUER}{path}")] = json

    def last(self, method: str, path: str) -> httpx2.Request:
        url = f"{ISSUER}{path}"
        return [r for r in self.requests if r.method == method and str(r.url).split("?")[0] == url][
            -1
        ]

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        self.requests.append(request)
        key = (request.method, str(request.url).split("?")[0])
        if key not in self.routes:
            return httpx2.Response(404, request=request)
        return httpx2.Response(200, json=self.routes[key], request=request)


@pytest.fixture
def issuer(monkeypatch):
    fake = FakeIssuer()
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", fake.handle)
    fake.respond(
        "GET",
        "/.well-known/openid-configuration",
        {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/oauth/v2/authorize",
            "token_endpoint": f"{ISSUER}/oauth/v2/token",
            "userinfo_endpoint": f"{ISSUER}/oidc/v1/userinfo",
            "jwks_uri": f"{ISSUER}/oauth/v2/keys",
            "end_session_endpoint": f"{ISSUER}/oidc/v1/end_session",
            "id_token_signing_alg_values_supported": ["RS256"],
        },
    )
    fake.respond("GET", "/oauth/v2/keys", {"keys": [KEY.as_dict(private=False)]})
    return fake


def _client(required_group: str | None = "savetube-admin") -> TestClient:
    settings = Settings(
        public_url="http://testserver",
        session_secret="test-secret",
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_required_group=required_group,
    )
    return TestClient(create_app(settings))


def _login(client: TestClient, issuer: FakeIssuer, groups: list[str], next: str = "/videos"):
    r = client.get("/auth/login", params={"next": next}, follow_redirects=False)
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/oauth/v2/authorize"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == "http://testserver/auth/callback"
    assert q["code_challenge_method"] == "S256"
    assert q["scope"] == "openid profile email"

    issuer.respond(
        "POST",
        "/oauth/v2/token",
        {
            "access_token": "at",
            "token_type": "Bearer",
            "expires_in": 300,
            "id_token": _id_token(q["nonce"]),
        },
    )
    issuer.respond(
        "GET",
        "/oidc/v1/userinfo",
        {"sub": "u1", "name": "Kim", "email": "k@example.test", "groups": groups},
    )
    r = client.get(
        "/auth/callback", params={"code": "c1", "state": q["state"]}, follow_redirects=False
    )
    body = parse_qs(issuer.last("POST", "/oauth/v2/token").content.decode())
    return r, body


def test_unauthenticated_api_is_401(issuer):
    with _client() as client:
        assert client.get("/api/me").status_code == 401
        assert client.get("/api/videos").status_code == 401
        assert client.get("/healthz").status_code == 200


def test_login_sets_session_and_logout_ends_it(issuer):
    with _client() as client:
        r, token_body = _login(client, issuer, ["savetube-admin", "internal-viewer"])
        assert r.status_code == 303
        assert r.headers["location"] == "/videos"
        # Public client: PKCE verifier, no secret.
        assert "code_verifier" in token_body
        assert "client_secret" not in token_body
        assert token_body["client_id"] == [CLIENT_ID]

        me = client.get("/api/me")
        assert me.status_code == 200
        assert me.json() == {
            "sub": "u1",
            "name": "Kim",
            "email": "k@example.test",
            "groups": ["savetube-admin", "internal-viewer"],
        }

        r = client.post("/auth/logout", follow_redirects=False)
        assert r.status_code == 303
        loc = urlsplit(r.headers["location"])
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{ISSUER}/oidc/v1/end_session"
        assert parse_qs(loc.query) == {
            "client_id": [CLIENT_ID],
            "post_logout_redirect_uri": ["http://testserver/"],
        }
        assert client.get("/api/me").status_code == 401


def test_user_without_required_group_is_refused(issuer):
    with _client() as client:
        r, _ = _login(client, issuer, ["internal-viewer"])
        assert r.status_code == 403
        assert client.get("/api/me").status_code == 401


def test_no_required_group_admits_any_issued_user(issuer):
    with _client(required_group=None) as client:
        r, _ = _login(client, issuer, [])
        assert r.status_code == 303
        assert client.get("/api/me").json()["groups"] == []


def test_callback_with_forged_state_is_rejected(issuer, caplog):
    with _client() as client:
        client.get("/auth/login", follow_redirects=False)
        with caplog.at_level(logging.WARNING, logger="savetube"):
            r = client.get(
                "/auth/callback",
                params={"code": "c1", "state": "forged"},
                headers={"user-agent": "Firefox/1"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert r.headers["location"] == "/?auth_error=mismatching_state"
        assert client.get("/api/me").status_code == 401
    [line] = _events(caplog, "auth.failed")
    assert _fields(line) == {
        "error": "mismatching_state",
        "description": "CSRF Warning! State not equal in request and response.",
        "cookie": "yes",
        "states": 1,
        "signed_in": "no",
        "agent": "Firefox/1",
    }


def test_callback_without_session_cookie_logs_no_cookie(issuer, caplog):
    with _client() as client:
        r = client.get("/auth/login", follow_redirects=False)
        state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
        client.cookies.clear()
        with caplog.at_level(logging.WARNING, logger="savetube"):
            r = client.get(
                "/auth/callback",
                params={"code": "c1", "state": state},
                follow_redirects=False,
            )
        assert r.headers["location"] == "/?auth_error=mismatching_state"
    [line] = _events(caplog, "auth.failed")
    assert (_fields(line)["cookie"], _fields(line)["states"]) == ("no", 0)


def test_repeated_callback_keeps_the_session(issuer, caplog):
    with _client() as client:
        r, _ = _login(client, issuer, ["savetube-admin"])
        assert r.status_code == 303
        with caplog.at_level(logging.WARNING, logger="savetube"):
            r = client.get(
                "/auth/callback", params={"code": "c1", "state": "used"}, follow_redirects=False
            )
        assert r.headers["location"] == "/?auth_error=mismatching_state"
        assert client.get("/api/me").status_code == 200
    [line] = _events(caplog, "auth.failed")
    assert _fields(line)["signed_in"] == "yes"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/videos?type=lives", "/videos?type=lives"),
        (None, "/"),
        ("https://evil.test/", "/"),
        ("//evil.test/", "/"),
        ("/\\evil.test/", "/"),
    ],
)
def test_next_is_same_origin_only(value, expected):
    assert _safe_next(value) == expected


def test_confidential_client_sends_basic_auth(issuer):
    settings = Settings(
        public_url="http://testserver",
        session_secret="s",
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret="shh",
    )
    with TestClient(create_app(settings)) as client:
        r = client.get("/auth/login", follow_redirects=False)
        q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
        issuer.respond(
            "POST",
            "/oauth/v2/token",
            {"access_token": "at", "token_type": "Bearer", "id_token": _id_token(q["nonce"])},
        )
        issuer.respond("GET", "/oidc/v1/userinfo", {"sub": "u1"})
        r = client.get(
            "/auth/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False
        )
        assert r.status_code == 303
        auth = issuer.last("POST", "/oauth/v2/token").headers["authorization"]
        assert auth == "Basic " + base64.b64encode(f"{CLIENT_ID}:shh".encode()).decode()
