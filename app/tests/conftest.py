import re

import pytest

from sagent.config import Config
from sagent.core import users
from sagent.web import create_app

PASSWORD = "correct-horse-battery"  # check_secrets: allow (test fixture)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("SAGENT_SECRET_KEY", raising=False)
    cfg = Config(home=tmp_path / "home", host="127.0.0.1", port=7832, trust_proxy=False, secure_cookies=False)
    return create_app(cfg, testing=True)


@pytest.fixture
def make_user(app):
    def _make(username, role="member", must_change=False):
        return users.create(users.SYSTEM, username, PASSWORD, role=role, must_change_password=must_change)

    return _make


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "proj1").mkdir(parents=True)
    (root / "proj2").mkdir()
    return root


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class Browser:
    """Flask test client wrapper that tracks the CSRF token like a browser."""

    def __init__(self, client):
        self.c = client
        self.csrf = ""

    def get(self, url, **kw):
        resp = self.c.get(url, **kw)
        if resp.mimetype == "text/html":
            m = CSRF_RE.search(resp.get_data(as_text=True))
            if m:
                self.csrf = m.group(1)
        return resp

    def post(self, url, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf:
            if not self.csrf:
                self.get("/login")
            data.setdefault("csrf_token", self.csrf)
        return self.c.post(url, data=data, **kw)

    def login(self, username, password=PASSWORD):
        self.get("/login")
        resp = self.post("/login", {"username": username, "password": password})
        self.get("/account/password")  # refresh csrf token for the new session
        return resp


@pytest.fixture
def browser(app):
    return Browser(app.test_client())


@pytest.fixture
def new_browser(app):
    return lambda: Browser(app.test_client())


FAKES = __import__("pathlib").Path(__file__).parent / "fakes"


@pytest.fixture
def fake_agents(monkeypatch, tmp_path):
    """Fake claude/codex on PATH and an isolated tmux socket per test."""
    import subprocess
    import uuid

    monkeypatch.setenv("PATH", f"{FAKES}:{__import__('os').environ['PATH']}")
    sock = f"sagent-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("SAGENT_TMUX_SOCKET", sock)
    yield
    subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)


@pytest.fixture
def team_project(app, make_user, workspace, fake_agents):
    from sagent.core import projects

    admin = make_user("admin1", "admin")
    owner = make_user("olivia")
    dev = make_user("dev1")
    dev2 = make_user("dev2")
    viewer = make_user("vic")
    p = projects.create(admin, "Team", str(workspace / "proj1"))
    for name, role in (("olivia", "owner"), ("dev1", "developer"), ("dev2", "developer"), ("vic", "viewer")):
        projects.set_member(admin, p.slug, name, role)
    return dict(admin=admin, owner=owner, dev=dev, dev2=dev2, viewer=viewer, project=p)


@pytest.fixture
def git_project(team_project, workspace):
    """Turn proj1 into a git repository with one commit."""
    import subprocess

    root = workspace / "proj1"
    (root / "app.py").write_text("print('v1')\n")
    env_args = ["-c", "user.email=test@example.com", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), *env_args, "commit", "-qm", "init"], check=True)
    return root


@pytest.fixture
def live_server(app):
    """Serve the test app over HTTP for real-browser tests."""
    import threading

    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def browser_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"chromium not available: {exc}")
        page = b.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.csp_errors = errors
        yield page
        b.close()
