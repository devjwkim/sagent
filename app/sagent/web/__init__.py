"""Flask application factory. Routes stay thin; logic lives in sagent.core."""
from __future__ import annotations

from datetime import timedelta

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from sagent import __version__, db
from sagent.config import Config
from sagent.core import audit, keystore, rbac, runs
from sagent.core.errors import Conflict, Forbidden, NotFound, SagentError, ValidationError
from sagent.web import filters, security

# Project sub-navigation (endpoint, label); feature phases append to this.
PROJECT_TABS: list[tuple[str, str]] = [
    ("home.project_overview", "개요"),
    ("loops.loop_list", "Loops"),
    ("runs.run_list", "Runs"),
    ("tests.test_list", "Tests"),
    ("reviews.review_list", "Reviews"),
    ("usage.project_usage", "Usage"),
    ("usage.prompt_list", "Prompts"),
    ("harness.harness_view", "Harness"),
    ("home.project_members", "멤버"),
]

# Endpoints that record their own, more specific audit entry.
_SELF_AUDITED = {"auth.login"}


def create_app(config: Config | None = None, *, testing: bool = False) -> Flask:
    config = config or Config()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.secret_key(),
        SESSION_COOKIE_NAME="sagent_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.secure_cookies,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        TESTING=testing,
        SAGENT=config,
    )
    if config.trust_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_db(config.db_path)
    keystore.configure(config.keystore_path)
    runs.configure(config.runs_dir)
    from sagent.core import usage

    usage.seed_prices()

    security.init_app(app)
    filters.register(app)

    from sagent.web.routes import admin, auth, harness, home
    from sagent.web.routes import loops as loops_routes
    from sagent.web.routes import runs as runs_routes
    from sagent.web.routes import reviews as reviews_routes
    from sagent.web.routes import tests as tests_routes
    from sagent.web.routes import usage as usage_routes

    app.register_blueprint(auth.bp)
    app.register_blueprint(home.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(harness.bp)
    app.register_blueprint(runs_routes.bp)
    from flask_sock import Sock

    app.config.setdefault("SOCK_SERVER_OPTIONS", {"ping_interval": 25})
    runs_routes.register_ws(Sock(app))
    app.register_blueprint(loops_routes.bp)
    app.register_blueprint(tests_routes.bp)
    app.register_blueprint(reviews_routes.bp)
    app.register_blueprint(usage_routes.bp)
    from sagent.web.routes import api as api_routes

    app.register_blueprint(api_routes.bp)

    @app.context_processor
    def _ctx():
        return {
            "current_user": g.get("user"),
            "app_version": __version__,
            "role_labels": rbac.ROLE_LABELS,
            "project_tabs": PROJECT_TABS,
        }

    @app.after_request
    def _audit(resp):
        if request.method not in security.SAFE_METHODS and request.endpoint not in _SELF_AUDITED:
            audit.record(
                "http." + request.method.lower(),
                g.get("user"),
                method=request.method,
                path=request.path,
                ip=security.client_ip(),
                status=resp.status_code,
            )
        return resp

    def _error(status: int, message: str):
        if security.wants_json():
            return jsonify(error=message), status
        return render_template("error.html", status=status, message=message), status

    @app.errorhandler(NotFound)
    def _nf(exc):
        return _error(404, str(exc) or "찾을 수 없습니다.")

    @app.errorhandler(Forbidden)
    def _fb(exc):
        return _error(403, str(exc) or "권한이 없습니다.")

    @app.errorhandler(ValidationError)
    @app.errorhandler(Conflict)
    def _bad(exc):
        return _error(400, str(exc))

    @app.errorhandler(SagentError)
    def _generic(exc):
        return _error(400, str(exc))

    @app.errorhandler(HTTPException)
    def _http(exc):
        return _error(exc.code or 500, exc.description or exc.name)

    return app
