# Copyright 2026 The skyctl Authors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
skyctl.platform.auth
====================
Inbound authentication for the HTTP surface (/mcp, /runs, /invoke, /tick),
as a chain of methods tried in order. Configure with SKYCTL_AUTH:

    method        who calls with it                               settings
    -----------   ---------------------------------------------   -----------------------------------------
    bearer        humans, MCP clients, scripts                    SKYCTL_HTTP_TOKEN (+ SKYCTL_APPROVER_TOKEN)
    google-oidc   Cloud Run IAM callers, Pub/Sub push,            SKYCTL_OIDC_AUDIENCE, SKYCTL_OIDC_ALLOWED
                  Cloud Tasks, Cloud Scheduler (ID tokens)
    entra-oidc    Event Grid (Entra auth), Entra apps,            AZURE_TENANT_ID, SKYCTL_OIDC_AUDIENCE,
                  managed identities                              SKYCTL_OIDC_ALLOWED
    oidc          any other issuer (Cognito, Okta, k8s SA…)       SKYCTL_OIDC_ISSUER, SKYCTL_OIDC_AUDIENCE,
                                                                  SKYCTL_OIDC_JWKS_URL (optional)
    aws-alb       an ALB with an authenticate-oidc action         AWS_REGION, SKYCTL_ALB_ARN
                  (x-amzn-oidc-data, ES256, signer-checked)
    easyauth      Container Apps / App Service Easy Auth          (none; trusts X-MS-CLIENT-PRINCIPAL-*)
    platform      the platform authenticates every request        (none; e.g. Cloud Run IAM, Lambda URL
                  before it reaches the app                       AWS_IAM)

`easyauth` and `platform` trust the platform, not the request: enable them
only when platform auth is enforced on every route, because the app cannot
tell a forged header from a real one. SKYCTL_OIDC_ALLOWED restricts
accepted subjects/emails/object-ids; set it, because "any valid Google
token for this audience" includes tokens from other projects' identities.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .base import _header


class AuthError(Exception):
    """Credentials were presented but are invalid (→ 401, do not try further methods)."""


@dataclass
class Principal:
    subject: str
    method: str
    issuer: str | None = None
    claims: dict = field(default_factory=dict)


class Authenticator:
    name = "base"

    def authenticate(self, headers: Mapping[str, str]) -> Principal | None:
        """A Principal, None if this method does not apply, or raise AuthError."""
        raise NotImplementedError


def _bearer(headers: Mapping[str, str]) -> str | None:
    scheme, _, token = _header(headers, "authorization").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2


class StaticBearer(Authenticator):
    """
    Static bearer tokens, each mapped to a subject. SKYCTL_HTTP_TOKEN is the
    "static-token" (agents, MCP clients); SKYCTL_APPROVER_TOKEN, if set, is
    the "approver-token", the only static credential that may approve plans.
    """

    name = "bearer"

    def __init__(self, token: str = "", approver_token: str = ""):
        self.tokens = {t: subj for t, subj in ((token, "static-token"), (approver_token, "approver-token")) if t}
        if not self.tokens:
            raise ValueError("bearer auth needs SKYCTL_HTTP_TOKEN")
        if token and approver_token and token == approver_token:
            raise ValueError("SKYCTL_APPROVER_TOKEN must differ from SKYCTL_HTTP_TOKEN")

    def authenticate(self, headers):
        presented = _bearer(headers)
        if presented is None or _looks_like_jwt(presented):
            return None  # leave JWTs to the OIDC methods
        for token, subject in self.tokens.items():
            if hmac.compare_digest(presented, token):
                return Principal(subject, self.name)
        raise AuthError("invalid bearer token")


class OidcJwt(Authenticator):
    """Verify a JWT bearer token against an issuer's JWKS."""

    name = "oidc"

    def __init__(
        self,
        issuers: Sequence[str],
        audience,
        jwks_url: str | None = None,
        allowed: Sequence[str] | None = None,
        algorithms: Sequence[str] = ("RS256",),
        leeway: int = 60,
        key_resolver: Callable[[str], object] | None = None,
        name: str | None = None,
    ):
        audiences = [a.strip() for a in (audience.split(",") if isinstance(audience, str) else audience) if a.strip()]
        if not audiences:
            raise ValueError("OIDC auth needs SKYCTL_OIDC_AUDIENCE")
        self.issuers = tuple(issuers)
        self.audience = audiences  # any of them (e.g. the service URL and its /invoke URL)
        self.jwks_url = jwks_url
        self.allowed = {a.strip().lower() for a in (allowed or []) if a.strip()}
        self.algorithms = list(algorithms)
        self.leeway = leeway
        self._key_resolver = key_resolver
        self._jwks_client: Any = None
        if name:
            self.name = name

    def _key_for(self, token: str):
        if self._key_resolver is not None:
            return self._key_resolver(token)
        import jwt

        if self._jwks_client is None:
            url = self.jwks_url or self._discover_jwks()
            self._jwks_client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)
        return self._jwks_client.get_signing_key_from_jwt(token).key

    def _discover_jwks(self) -> str:
        import httpx

        issuer = self.issuers[0].rstrip("/")
        if not issuer.startswith("http"):
            issuer = f"https://{issuer}"
        return httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=10).json()["jwks_uri"]

    def authenticate(self, headers):
        token = _bearer(headers)
        if token is None or not _looks_like_jwt(token):
            return None
        import jwt

        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as e:
            raise AuthError(f"malformed token: {e}") from e
        if unverified.get("iss") not in self.issuers:
            return None  # another method's issuer; let the chain continue
        try:
            claims = jwt.decode(
                token,
                self._key_for(token),
                algorithms=self.algorithms,
                audience=self.audience,
                leeway=self.leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from e
        identities = {str(claims.get(k, "")).lower() for k in ("sub", "email", "oid", "appid", "azp")} - {""}
        if self.allowed and not (identities & self.allowed):
            raise AuthError("token subject is not in SKYCTL_OIDC_ALLOWED")
        subject = claims.get("email") or claims.get("oid") or claims.get("sub", "")
        return Principal(subject, self.name, claims.get("iss"), claims)


def google_oidc(audience: str, allowed=None, **kw) -> OidcJwt:
    return OidcJwt(
        ("https://accounts.google.com", "accounts.google.com"),
        audience,
        jwks_url="https://www.googleapis.com/oauth2/v3/certs",
        allowed=allowed,
        name="google-oidc",
        **kw,
    )


def entra_oidc(tenant: str, audience: str, allowed=None, **kw) -> OidcJwt:
    if not tenant:
        raise ValueError("entra-oidc needs AZURE_TENANT_ID")
    return OidcJwt(
        (f"https://login.microsoftonline.com/{tenant}/v2.0", f"https://sts.windows.net/{tenant}/"),
        audience,
        jwks_url=f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
        allowed=allowed,
        name="entra-oidc",
        **kw,
    )


class AwsAlbOidc(Authenticator):
    """
    An ALB `authenticate-oidc` action signs user claims into x-amzn-oidc-data
    (ES256). Verify the signature with the regional ALB key and check that
    the `signer` header field is *our* load balancer.
    """

    name = "aws-alb"

    def __init__(self, region: str, alb_arn: str, key_resolver: Callable[[str], object] | None = None):
        if not region or not alb_arn:
            raise ValueError("aws-alb auth needs AWS_REGION and SKYCTL_ALB_ARN")
        self.region, self.alb_arn = region, alb_arn
        self._key_resolver = key_resolver
        self._cache: dict[str, object] = {}

    def _key(self, kid: str):
        if self._key_resolver is not None:
            return self._key_resolver(kid)
        if kid not in self._cache:
            import httpx

            url = f"https://public-keys.auth.elb.{self.region}.amazonaws.com/{kid}"
            self._cache[kid] = httpx.get(url, timeout=10).text
        return self._cache[kid]

    def authenticate(self, headers):
        data = _header(headers, "x-amzn-oidc-data")
        if not data:
            return None
        import jwt

        try:
            header = json.loads(base64.urlsafe_b64decode(data.split(".")[0] + "=="))
        except Exception as e:
            raise AuthError(f"malformed ALB token: {e}") from e
        if header.get("signer") != self.alb_arn:
            raise AuthError("ALB token was not signed by this load balancer")
        try:
            claims = jwt.decode(data, self._key(header["kid"]), algorithms=["ES256"], options={"verify_aud": False})
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid ALB token: {e}") from e
        return Principal(claims.get("email") or claims.get("sub", ""), self.name, header.get("iss"), claims)


class EasyAuth(Authenticator):
    name = "easyauth"

    def authenticate(self, headers):
        name = _header(headers, "x-ms-client-principal-name") or _header(headers, "x-ms-client-principal-id")
        if not name:
            return None
        return Principal(name, self.name, claims={"idp": _header(headers, "x-ms-client-principal-idp")})


class PlatformTrusted(Authenticator):
    name = "platform"

    def authenticate(self, headers):
        return Principal("platform-authenticated", self.name)


class AuthChain:
    def __init__(self, methods: Sequence[Authenticator]):
        self.methods = list(methods)

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        if not self.methods:
            raise AuthError("no authentication configured (set SKYCTL_HTTP_TOKEN or SKYCTL_AUTH)")
        for method in self.methods:
            principal = method.authenticate(headers)
            if principal is not None:
                return principal
        raise AuthError("no accepted credentials presented")

    @property
    def names(self) -> list[str]:
        return [m.name for m in self.methods]


def build_auth_chain(spec: str | None = None, env: Mapping[str, str] | None = None) -> AuthChain:
    env = os.environ if env is None else env
    raw = spec if spec is not None else env.get("SKYCTL_AUTH", "")
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    if not names:
        # Backwards compatible default: the static bearer token, or explicit anonymous.
        if env.get("SKYCTL_HTTP_TOKEN") or env.get("SKYCTL_APPROVER_TOKEN"):
            names = ["bearer"]
        elif env.get("SKYCTL_HTTP_ALLOW_ANON", "").lower() in ("1", "true", "yes"):
            names = ["platform"]
    allowed = [a for a in env.get("SKYCTL_OIDC_ALLOWED", "").split(",") if a.strip()]
    audience = env.get("SKYCTL_OIDC_AUDIENCE", "")
    methods: list[Authenticator] = []
    for name in names:
        if name == "bearer":
            methods.append(StaticBearer(env.get("SKYCTL_HTTP_TOKEN", ""), env.get("SKYCTL_APPROVER_TOKEN", "")))
        elif name == "google-oidc":
            methods.append(google_oidc(audience, allowed))
        elif name == "entra-oidc":
            methods.append(entra_oidc(env.get("AZURE_TENANT_ID", ""), audience, allowed))
        elif name == "oidc":
            methods.append(
                OidcJwt(
                    [env.get("SKYCTL_OIDC_ISSUER", "")],
                    audience,
                    jwks_url=env.get("SKYCTL_OIDC_JWKS_URL") or None,
                    allowed=allowed,
                )
            )
        elif name == "aws-alb":
            methods.append(AwsAlbOidc(env.get("AWS_REGION", ""), env.get("SKYCTL_ALB_ARN", "")))
        elif name == "easyauth":
            methods.append(EasyAuth())
        elif name == "platform":
            methods.append(PlatformTrusted())
        else:
            raise ValueError(f"Unknown auth method {name!r} in SKYCTL_AUTH")
    return AuthChain(methods)


def plan_approvers(env: Mapping[str, str] | None = None) -> set[str]:
    """Principals allowed to approve plans: the approver token plus SKYCTL_PLAN_APPROVERS (subjects/emails)."""
    env = os.environ if env is None else env
    approvers = {a.strip().lower() for a in env.get("SKYCTL_PLAN_APPROVERS", "").split(",") if a.strip()}
    if env.get("SKYCTL_APPROVER_TOKEN"):
        approvers.add("approver-token")
    return approvers
