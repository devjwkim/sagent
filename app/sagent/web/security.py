"""Session user loading, CSRF, security headers and access decorators."""
from __future__ import annotations

import hmac
import secrets
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    Flask, abort, g, jsonify, redirect, request, session, url_for,
)

from sagent.core import projects, users
from sagent.core.errors import NotFound

CSRF_SESSION_KEY = "_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def client_ip() -> str:
    return (request.remote_addr or "")[:64]


def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def wants_json() -> bool:
    return (
        request.headers.get("HX-Request") == "true"
        or request.accept_mimetypes.best == "application/json"
        or request.is_json
    )


def safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return url_for("home.dashboard")
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return url_for("home.dashboard")
    return target


def login_user(user: users.User) -> None:
    session.clear()  # prevent session fixation
    session.permanent = True
    session["uid"] = user.id
    session["epoch"] = user.session_epoch
    csrf_token()


def logout_user() -> None:
    session.clear()


def is_api_request() -> bool:
    return request.path.startswith("/api/")


def _load_api_user() -> None:
    """The JSON API authenticates only with bearer tokens, never the session
    cookie — so it needs no CSRF token and cannot be driven cross-site."""
    from sagent.core import tokens, users as users_mod

    g.api_scope = None
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return
    ip = client_ip()
    if users_mod.is_locked("", ip):
        return
    res = tokens.authenticate(auth[7:].strip())
    if res is None:
        from sagent import db

        db.execute("INSERT INTO login_attempts (username, ip, success, created_at) VALUES ('', ?, 0, ?)",
                   (ip, db.utcnow()))
        return
    g.user, g.api_scope = res


def _load_user() -> None:
    g.user = None
    if is_api_request():
        _load_api_user()
        return
    uid = session.get("uid")
    if uid is None:
        return
    try:
        user = users.get(uid)
    except NotFound:
        session.clear()
        return
    if not user.is_active or user.session_epoch != session.get("epoch"):
        session.clear()
        return
    g.user = user


def _check_csrf() -> None:
    if request.method in SAFE_METHODS or is_api_request():
        return
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get(CSRF_SESSION_KEY, "")
    if not expected or not sent or not hmac.compare_digest(sent, expected):
        abort(400, description="CSRF token missing or invalid. Reload the page and try again.")


_PASSWORD_EXEMPT = {"auth.change_password", "auth.logout", "static"}


def init_app(app: Flask) -> None:
    @app.before_request
    def _before():
        _load_user()
        _check_csrf()
        user = g.user
        if (
            user is not None
            and user.must_change_password
            and request.endpoint not in _PASSWORD_EXEMPT
        ):
            if is_api_request():
                return jsonify(error="password change required before using the API"), 403
            return redirect(url_for("auth.change_password"))
        return None

    @app.after_request
    def _headers(resp):
        csp = CSP
        if g.get("csp_relax_styles"):  # only pages embedding xterm.js
            csp = csp.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.endpoint != "static":
            resp.headers.setdefault("Cache-Control", "no-store")
        if app.config.get("SESSION_COOKIE_SECURE"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    app.jinja_env.globals["csrf_token"] = csrf_token


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.get("user") is None:
            if wants_json():
                return jsonify(error="login required"), 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not g.user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def project_perm(perm: str = "project.view"):
    """Resolve <slug> into g.project / g.project_role, enforcing `perm`.
    Invisible projects 404; visible-but-forbidden 403 (via core errors)."""

    def deco(view):
        @wraps(view)
        @login_required
        def wrapper(*args, slug, **kwargs):
            g.project, g.project_role = projects.get(g.user, slug, perm)
            return view(*args, slug=slug, **kwargs)

        return wrapper

    return deco
