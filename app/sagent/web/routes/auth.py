from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from sagent.core import users
from sagent.core.errors import SagentError, ValidationError
from sagent.web.security import client_ip, login_required, login_user, logout_user, safe_next

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("home.dashboard"))
    no_users = users.count_users() == 0
    if request.method == "POST" and not no_users:
        user = users.authenticate(
            request.form.get("username", ""), request.form.get("password", ""), client_ip()
        )
        if user is None:
            flash("아이디 또는 비밀번호가 올바르지 않거나, 잠시 로그인이 제한되었습니다.", "error")
            return render_template("login.html", no_users=no_users), 401
        login_user(user)
        return redirect(safe_next(request.args.get("next")))
    return render_template("login.html", no_users=no_users)


@bp.route("/setup", methods=["GET", "POST"])
def first_setup():
    """One-time page to create the first admin (needs the setup link's token)."""
    from flask import abort, current_app, session

    from sagent.core import setup

    home = current_app.config["SAGENT"].home
    if not setup.needed():
        abort(404)
    if request.method == "GET" and request.args.get("token"):
        # keep the token out of the address bar and browser history
        session["setup_token"] = request.args["token"]
        return redirect(url_for("auth.first_setup"))
    token = session.get("setup_token") or request.form.get("token", "")
    if not setup.valid(home, token):
        abort(404)
    error = None
    if request.method == "POST":
        f = request.form
        if f.get("password", "") != f.get("confirm", ""):
            error = "비밀번호 확인이 일치하지 않습니다."
        else:
            try:
                admin = setup.complete(home, token, f.get("username", ""), f.get("password", ""),
                                       f.get("display_name", ""))
            except SagentError as exc:
                error = str(exc)
            else:
                login_user(admin)
                flash("관리자 계정을 만들었습니다. 이제 서버 설정에서 프로젝트 경로를 지정하세요.", "ok")
                return redirect(url_for("admin.server_settings"))
    return render_template("setup.html", error=error, min_len=users.MIN_PASSWORD_LEN)


@bp.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/account/tokens", methods=["GET", "POST"])
@login_required
def api_tokens():
    from sagent.core import tokens

    new_token = None
    if request.method == "POST":
        try:
            if request.form.get("action") == "revoke":
                tokens.revoke(g.user, int(request.form.get("token_id", "0")))
                flash("토큰을 폐기했습니다.", "ok")
            else:
                days = request.form.get("days", "90")
                new_token, _ = tokens.create(g.user, request.form.get("name", ""), request.form.get("scope", "read"),
                                             int(days) if days.isdigit() and days != "0" else None)
        except (ValidationError, ValueError) as exc:
            flash(str(exc) or "잘못된 요청입니다.", "error")
    return render_template("account/tokens.html", items=tokens.list_for(g.user), new_token=new_token)


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        if request.form.get("new", "") != request.form.get("confirm", ""):
            flash("새 비밀번호 확인이 일치하지 않습니다.", "error")
        else:
            try:
                user = users.change_password(
                    g.user, request.form.get("current", ""), request.form.get("new", ""), client_ip()
                )
            except ValidationError as exc:
                flash(str(exc), "error")
            else:
                login_user(user)  # epoch changed: re-issue this session
                flash("비밀번호를 변경했습니다.", "ok")
                return redirect(url_for("home.dashboard"))
    return render_template("account/password.html", min_len=users.MIN_PASSWORD_LEN)
