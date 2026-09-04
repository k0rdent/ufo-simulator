"""Operator JWT mint + cache — Python port of ansible/k0r.sh k0r_login/k0r_token."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException


def _jwt_exp(token: str) -> int | None:
    """Return JWT exp claim, or None if the token is not a readable JWT."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except (ValueError, TypeError, json.JSONDecodeError, OSError):
        return None


def _cache_path() -> Path:
    return Path(os.environ.get("K0R_TOKEN_FILE", "/tmp/k0r-token"))


def _slack_sec() -> int:
    return int(os.environ.get("TOKEN_SLACK_SEC", "60"))


def _api_base() -> str:
    return (os.environ.get("API_BASE") or os.environ.get("BASE") or "").rstrip("/")


def _login_redirect_uri() -> str:
    explicit = os.environ.get("K0R_LOGIN_REDIRECT_URI")
    if explicit:
        return explicit
    base = _api_base()
    if not base:
        raise RuntimeError("API_BASE (or K0R_LOGIN_REDIRECT_URI) is required to mint a token")
    return f"{base}/v1/regions/global/auth/callback"


def _cached_token_if_fresh() -> str | None:
    """Same freshness rule as bash k0r_token: exp - now > TOKEN_SLACK_SEC."""
    cache = _cache_path()
    if not cache.is_file():
        return None
    try:
        tok = cache.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not tok:
        return None
    exp = _jwt_exp(tok)
    if exp is None:
        return None
    if exp - int(time.time()) > _slack_sec():
        return tok
    return None


def _write_cache(token: str) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _kube_core() -> client.CoreV1Api:
    kubeconfig = os.environ.get("KUBECONFIG")
    if kubeconfig:
        config.load_kube_config(config_file=kubeconfig)
    else:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
    return client.CoreV1Api()


def _service_cluster_ip(core: client.CoreV1Api, namespace: str, name: str) -> str:
    try:
        svc = core.read_namespaced_service(name, namespace)
    except ApiException as e:
        raise RuntimeError(
            f"k0r_login: get svc {namespace}/{name} failed: {e.status} {e.reason}"
        ) from e
    ip = (svc.spec.cluster_ip if svc.spec else None) or ""
    if not ip or ip in ("None", "none"):
        raise RuntimeError(f"k0r_login: service {namespace}/{name} has no clusterIP")
    return ip


def _rewrite_mock_oauth_host(authorization_url: str, namespace: str, mock_ip: str) -> str:
    """Replace in-cluster mock-oauth2 DNS with the service ClusterIP (as bash sed does)."""
    # http://mock-oauth2-server.<ns>.svc:8080 → http://<mock_ip>:8080
    pattern = rf"http://mock-oauth2-server\.{re.escape(namespace)}\.svc(?::8080)?"
    return re.sub(pattern, f"http://{mock_ip}:8080", authorization_url)


def _extract_access_token(location: str) -> str | None:
    """Pull access_token from a callback Location (#fragment or ?query)."""
    if not location:
        return None
    # Prefer fragment (OIDC implicit-style callback used by the lab).
    if "#" in location:
        frag = location.split("#", 1)[1]
        for part in frag.split("&"):
            if part.startswith("access_token="):
                return unquote(part.split("=", 1)[1])
    parsed = urlparse(location)
    if parsed.query:
        for part in parsed.query.split("&"):
            if part.startswith("access_token="):
                return unquote(part.split("=", 1)[1])
    # Bash fallback: strip up to access_token= anywhere in the string.
    m = re.search(r"[#?&]access_token=([^&]+)", location)
    if m:
        return unquote(m.group(1))
    if "access_token=" in location:
        return unquote(location.split("access_token=", 1)[1].split("&", 1)[0])
    return None


def k0r_login() -> str:
    """Mint a fresh operator JWT via auth + mock-oauth2 (same flow as bash k0r_login)."""
    ns = os.environ.get("K0R_NAMESPACE", "k0rdent-apis")
    email = os.environ.get("K0R_LOGIN_EMAIL", "admin@kind.test")
    client_id = os.environ.get("K0R_LOGIN_CLIENT_ID", "operator-portal")
    redirect_uri = _login_redirect_uri()

    core = _kube_core()
    auth_ip = _service_cluster_ip(core, ns, "auth")
    mock_ip = _service_cluster_ip(core, ns, "mock-oauth2-server")
    auth_base = f"http://{auth_ip}"

    session = requests.Session()
    init = session.post(
        f"{auth_base}/v1/regions/global/auth/login/initiate",
        json={"email": email, "redirectUri": redirect_uri, "clientId": client_id},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    init.raise_for_status()
    body = init.json()
    auth_url = body.get("authorizationUrl") or ""
    if not auth_url:
        raise RuntimeError(f"k0r_login: no authorizationUrl in init response: {body!r}")
    auth_url = _rewrite_mock_oauth_host(auth_url, ns, mock_ip)

    # Follow redirects manually so we can read Location fragments (curl -v -L).
    url = auth_url
    last_location = ""
    for _ in range(6):
        resp = session.get(url, allow_redirects=False, timeout=30)
        if resp.status_code not in (301, 302, 303, 307, 308):
            break
        loc = resp.headers.get("Location") or ""
        if not loc:
            break
        last_location = loc
        token = _extract_access_token(loc)
        if token:
            if token.count(".") < 2:
                raise RuntimeError("k0r_login: extracted value is not a JWT")
            return token
        # Resolve relative redirects against the current URL.
        url = urljoin(url, loc)
        # Drop fragment when continuing (server won't see it).
        parts = urlparse(url)
        if parts.fragment:
            url = urlunparse(parts._replace(fragment=""))

    raise RuntimeError(
        f"k0r_login: no token extracted. Last redirect: {last_location or url!r}"
    )


def mint_token() -> str:
    """Mint via k0r_login and write the JWT cache (bash k0r_token write path)."""
    tok = k0r_login()
    if tok.count(".") < 2:
        raise RuntimeError("k0r_login did not return a JWT")
    _write_cache(tok)
    return tok


def invalidate_cached_token() -> None:
    """Drop disk cache and env TOKEN so the next get_token() remints."""
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        pass
    os.environ.pop("TOKEN", None)


def force_refresh_token() -> str:
    """Always mint a new JWT (used after 401 or when callers demand a remint)."""
    invalidate_cached_token()
    return mint_token()


def get_token() -> str:
    """Return a usable operator JWT, refreshing when near expiry."""
    fresh = _cached_token_if_fresh()
    if fresh:
        return fresh

    # Env TOKEN may still be valid (e.g. exported manually).
    static = (os.environ.get("TOKEN") or "").strip()
    if static:
        exp = _jwt_exp(static)
        if exp is not None and exp - int(time.time()) > _slack_sec():
            return static

    return mint_token()


class RefreshingBearerAuth(requests.auth.AuthBase):
    """Attach a current Bearer token on every request (proactive refresh via get_token)."""

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {get_token()}"
        return request


class AuthedSession(requests.Session):
    """Session that keeps JWT fresh and remints once on HTTP 401."""

    def __init__(self) -> None:
        super().__init__()
        self.auth = RefreshingBearerAuth()

    def request(self, method, url, **kwargs):  # noqa: ANN001
        # Pop our private flag so it never reaches urllib3.
        allow_retry = kwargs.pop("_auth_retry", True)
        resp = super().request(method, url, **kwargs)
        if resp.status_code != 401 or not allow_retry:
            return resp
        # Server rejected the token — remint and retry the same call once.
        force_refresh_token()
        return self.request(method, url, _auth_retry=False, **kwargs)
