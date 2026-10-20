"""OpenID Connect login against Zitadel, authorization code with PKCE.

The backend is the OIDC client. The browser holds only a signed session cookie
with the user's claims. No token reaches the frontend.
"""

import logging
from typing import Annotated
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from savetube.config import Settings

SCOPE = "openid profile email"
SESSION_USER = "user"
SESSION_NEXT = "next"

log = logging.getLogger(__name__)


class User(BaseModel):
    sub: str
    name: str | None = None
    email: str | None = None
    groups: list[str] = []


def _safe_next(value: str | None) -> str:
    # A same-origin path only. `//host` and `/\host` are protocol-relative to a browser.
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    return value


def install(app: FastAPI, settings: Settings) -> None:
    if not settings.session_secret.get_secret_value():
        raise RuntimeError("SAVETUBE_SESSION_SECRET is empty")
    if not settings.oidc_client_id:
        raise RuntimeError("SAVETUBE_OIDC_CLIENT_ID is empty")

    oauth = OAuth()
    secret = settings.oidc_client_secret
    oauth.register(
        "zitadel",
        client_id=settings.oidc_client_id,
        client_secret=secret.get_secret_value() if secret else None,
        server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={
            "scope": SCOPE,
            "code_challenge_method": "S256",
            "token_endpoint_auth_method": "client_secret_basic" if secret else "none",
        },
    )
    client = oauth.create_client("zitadel")
    public_url = settings.public_url.rstrip("/")

    # Added last so it wraps everything, including the static files.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="savetube_session",
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=public_url.startswith("https://"),
    )

    router = APIRouter(prefix="/auth", include_in_schema=False)

    @router.get("/login")
    async def login(request: Request, next: str | None = None):
        request.session.pop(SESSION_USER, None)
        request.session[SESSION_NEXT] = _safe_next(next)
        return await client.authorize_redirect(request, f"{public_url}/auth/callback")

    @router.get("/callback")
    async def callback(request: Request):
        try:
            token = await client.authorize_access_token(request)
        except OAuthError as e:
            log.warning("login failed: %s: %s", e.error, e.description)
            return PlainTextResponse(f"login failed: {e.error}", status_code=400)
        claims = token["userinfo"]
        # Zitadel's flat `groups` claim comes from an Action on the userinfo
        # trigger. The userinfo endpoint carries it whatever the ID token settings.
        userinfo = await client.userinfo(token=token)
        groups = userinfo.get("groups") or claims.get("groups") or []
        if settings.oidc_required_group and settings.oidc_required_group not in groups:
            log.warning(
                "login refused: sub=%s lacks %s, groups=%s",
                claims.get("sub"),
                settings.oidc_required_group,
                groups,
            )
            request.session.clear()
            return PlainTextResponse("forbidden: missing required group", status_code=403)
        next_path = _safe_next(request.session.get(SESSION_NEXT))
        request.session.clear()
        request.session[SESSION_USER] = User(
            sub=claims["sub"],
            name=userinfo.get("name") or claims.get("name"),
            email=userinfo.get("email") or claims.get("email"),
            groups=groups,
        ).model_dump()
        return RedirectResponse(next_path, status_code=303)

    # POST, so a cross-site <img> cannot log the user out.
    @router.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        metadata = await client.load_server_metadata()
        # Zitadel honours post_logout_redirect_uri only when client_id or
        # id_token_hint names the client, and only if the URI is registered.
        query = urlencode(
            {"client_id": settings.oidc_client_id, "post_logout_redirect_uri": f"{public_url}/"}
        )
        return RedirectResponse(f"{metadata['end_session_endpoint']}?{query}", status_code=303)

    app.include_router(router)


def current_user(request: Request) -> User:
    data = request.session.get(SESSION_USER)
    if not data:
        raise HTTPException(status_code=401, detail="not logged in")
    return User.model_validate(data)


CurrentUser = Annotated[User, Depends(current_user)]
