"""`sagent` command line. Uses the same core as the web UI.

The CLI runs on the server machine with direct access to the database, so it
acts as the trusted local operator (users.SYSTEM) unless `--as` is given.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sagent import __version__, db
from sagent.config import Config
from sagent.core import keystore


def _bootstrap(args) -> Config:
    cfg = Config(home=args.home) if args.home else Config()
    db.init_db(cfg.db_path)
    keystore.configure(cfg.keystore_path)
    from sagent.core import runs

    runs.configure(cfg.runs_dir)
    from sagent.core import usage

    usage.seed_prices()
    return cfg


def _actor(args):
    from sagent.core import users

    name = getattr(args, "as_user", None)
    if not name:
        return users.SYSTEM
    user = users.find(name)
    if not user or not user.is_active:
        raise SystemExit(f"error: unknown or inactive user '{name}'")
    return user


def _read_password(args, prompt: str = "Password: ") -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(prompt)
    if getpass.getpass("Repeat: ") != first:
        raise SystemExit("error: passwords do not match")
    return first


# --- commands ---------------------------------------------------------------

def cmd_web(args) -> int:
    from sagent.web import create_app

    cfg = _bootstrap(args)
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.debug and not cfg.is_loopback:
        raise SystemExit("error: --debug is only allowed when binding to localhost")
    if not cfg.is_loopback:
        print(
            f"WARNING: listening on {cfg.host}. Put sagent behind an HTTPS reverse proxy and "
            "set SAGENT_TRUST_PROXY=1 / SAGENT_SECURE_COOKIES=1. Anyone who can log in as a "
            "developer can run commands as this OS user.",
            file=sys.stderr,
        )
    app = create_app(cfg)
    _redact_access_log()
    from sagent.core import users
    from sagent.web import workers

    # With the reloader only the child process (WERKZEUG_RUN_MAIN) runs workers.
    import os

    if not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        workers.start()

    from sagent.core import setup

    print(f"sagent {__version__} → http://{cfg.host}:{cfg.port}  (data: {cfg.home})", flush=True)
    token = setup.ensure_token(cfg.home)
    if token:
        print("\nFirst run: open this one-time link to create the administrator:", flush=True)
        print(f"  {setup.url(cfg.host, cfg.port, token)}", flush=True)
        lan = setup.lan_hint(cfg.host)
        if lan:
            print(f"  (from another machine: http://{lan}:{cfg.port}/setup?token={token})", flush=True)
        print("  or create it in a terminal: sagent user create-admin\n", flush=True)
    if args.waitress:
        from waitress import serve

        serve(app, host=cfg.host, port=cfg.port, threads=8)
    else:
        app.run(host=cfg.host, port=cfg.port, debug=args.debug, use_reloader=args.debug, threaded=True)
    return 0


def _redact_access_log() -> None:
    """Keep the one-time setup token out of the request log."""
    import logging
    import re

    class _Redact(logging.Filter):
        rx = re.compile(r"(token=)[^&\s\"]+")

        def filter(self, record):
            if record.args:
                record.args = tuple(self.rx.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
            if isinstance(record.msg, str):
                record.msg = self.rx.sub(r"\1***", record.msg)
            return True

    logging.getLogger("werkzeug").addFilter(_Redact())


def cmd_user_create(args, role: str) -> int:
    from sagent.core import users
    from sagent.core.errors import SagentError

    _bootstrap(args)
    username = args.username or input("Username: ").strip()
    password = _read_password(args)
    try:
        user = users.create(
            users.SYSTEM, username, password, role=role,
            display_name=args.display_name or "", must_change_password=False,
        )
    except SagentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created {user.role} '{user.username}' (id={user.id})")
    return 0


def cmd_user_list(args) -> int:
    from sagent.core import users

    _bootstrap(args)
    for u in users.list_all():
        state = "active" if u.is_active else "disabled"
        print(f"{u.id:>4}  {u.username:<24} {u.role:<7} {state:<8} last_login={u.last_login_at or '-'}")
    return 0


def cmd_user_reset(args) -> int:
    from sagent.core import users
    from sagent.core.errors import SagentError

    _bootstrap(args)
    target = users.find(args.username)
    if not target:
        print("error: no such user", file=sys.stderr)
        return 1
    try:
        users.reset_password(users.SYSTEM, target.id, _read_password(args, "New password: "))
    except SagentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"password reset for '{target.username}' (must change at next login)")
    return 0


def cmd_setup_url(args) -> int:
    from sagent.core import setup

    cfg = _bootstrap(args)
    host = args.host or cfg.host
    port = args.port or cfg.port
    token = setup.ensure_token(cfg.home)
    if not token:
        print("setup is complete (an account exists); log in at "
              f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/login")
        return 1
    print(setup.url(host, port, token))
    lan = setup.lan_hint(host)
    if lan:
        print(f"http://{lan}:{port}/setup?token={token}")
    return 0


def cmd_doctor(args) -> int:
    from sagent.core import doctor

    _bootstrap(args)
    failed = False
    for c in doctor.run_checks():
        mark = "OK  " if c.ok else ("FAIL" if c.required else "--  ")
        failed |= c.required and not c.ok
        print(f"{mark} {c.name:<10} {c.detail}")
    return 1 if failed else 0


def cmd_project_list(args) -> int:
    from sagent.core import projects

    _bootstrap(args)
    for p, role in projects.list_for(_actor(args), include_archived=args.all):
        print(f"{p.slug:<24} {p.lifecycle:<12} {p.primary_agent:<7} {role:<10} {p.path}")
    return 0


def cmd_scan(args) -> int:
    import json

    from sagent.core import scanner

    print(json.dumps(scanner.scan(args.path), indent=2, ensure_ascii=False))
    return 0


def _ask(prompt: str, default: str, choices: tuple[str, ...] | None = None, assume_yes: bool = False) -> str:
    if assume_yes or not sys.stdin.isatty():
        return default
    hint = f" [{'/'.join(choices)}]" if choices else ""
    while True:
        val = input(f"{prompt}{hint} ({default}): ").strip() or default
        if not choices or val in choices:
            return val
        print(f"  choose one of: {', '.join(choices)}")


def cmd_import(args) -> int:
    import os
    from pathlib import Path

    from sagent.core import harness, loopdef, projects, scanner
    from sagent.core.errors import SagentError

    _bootstrap(args)
    actor = _actor(args)
    path = os.path.realpath(args.path)
    name = args.name or Path(path).name
    scan = scanner.scan(path)
    print(f"Detected: languages={', '.join(scan['languages']) or '-'}; "
          f"frameworks={', '.join(scan['frameworks']) or '-'}; unit={scan['unit_test'] or '-'}; "
          f"e2e={scan['e2e'] or '-'}; git={'yes' if scan['git']['detected'] else 'no'}")
    y = args.yes
    coding = args.agent or _ask("Primary coding agent", "claude", projects.AGENTS, y)
    review = args.review_agent or _ask("AI review agent", "codex" if coding == "claude" else "claude",
                                       (*projects.AGENTS, "same"), y)
    loop = args.loop or _ask("Development loop", "standard", tuple(loopdef.TEMPLATES), y)
    try:
        project = projects.find_by_path(path) or projects.create(
            actor, name, path, primary_agent=coding, create_dir=args.create)
        projects.save_scan(actor, project, scan)
        result = harness.bootstrap(
            actor, project.slug, scan,
            {"coding_agent": coding, "review_agent": review, "loop": loop,
             "e2e": bool(scan["commands"].get("e2e"))},
            overwrite=args.overwrite,
        )
    except SagentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"project '{project.slug}' ready. written: {', '.join(result['written']) or '-'}; "
          f"kept: {', '.join(result['skipped']) or '-'}")
    for fname, d in result["docs"].items():
        if d["status"] == "exists":
            print(f"note: {fname} exists and was left unchanged")
    return 0


def _resolve_slug(args) -> str:
    import os

    from sagent.core import projects

    if args.project:
        return args.project
    p = projects.find_by_path(os.getcwd())
    if not p:
        raise SystemExit("error: not inside a registered project; pass --project <slug>")
    return p.slug


def cmd_run(args) -> int:
    from sagent.core import runs
    from sagent.core.errors import SagentError

    _bootstrap(args)
    actor = _actor(args)
    slug = _resolve_slug(args)
    prompt = args.prompt if args.prompt != "-" else sys.stdin.read()
    try:
        if args.command:
            run = runs.start_command(actor, slug, args.command)
        else:
            run = runs.start_agent(actor, slug, prompt or "", provider=args.agent,
                                   mode="interactive" if args.interactive else "auto",
                                   resume_session=args.resume)
    except SagentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"run #{run.id} {run.status} · tmux: {run.tmux_session}")
    if args.interactive or args.attach:
        import os

        from sagent.runtime import tmux

        os.execvp("tmux", tmux.attach_argv(run.tmux_session))
    if args.wait:
        run = runs.wait(run.id, timeout=args.timeout)
        _print_run(run)
        return 0 if run.status == "SUCCESS" else 2
    return 0


def _print_run(run) -> None:
    cost = f" ${run.cost_usd:.4f}" if run.cost_usd is not None else ""
    print(f"#{run.id:<5} {run.status:<10} {run.provider:<7} tokens in={run.input_tokens} "
          f"out={run.output_tokens}{cost}  {run.title[:60]}")
    if run.error:
        print(f"       error: {run.error}")


def cmd_status(args) -> int:
    from sagent.core import runs

    _bootstrap(args)
    runs.tick()
    actor = _actor(args)
    if args.run_id:
        run, _, _ = runs.get(actor, args.run_id)
        _print_run(run)
        return 0
    for item in runs.list_for_project(actor, _resolve_slug(args), limit=args.limit):
        _print_run(item)
    return 0


def cmd_attach(args) -> int:
    import os

    from sagent.core import runs
    from sagent.runtime import tmux

    _bootstrap(args)
    run, _, _ = runs.get(_actor(args), args.run_id, "terminal.view")
    if not run.is_active:
        print(runs.terminal(_actor(args), run.id)[1])
        return 0
    os.execvp("tmux", tmux.attach_argv(run.tmux_session))
    return 0


def cmd_stop(args) -> int:
    from sagent.core import runs

    _bootstrap(args)
    _print_run(runs.stop(_actor(args), args.run_id))
    return 0


def cmd_loop_list(args) -> int:
    from sagent.core import loops, projects

    _bootstrap(args)
    project, _ = projects.get(_actor(args), _resolve_slug(args))
    defs, default = loops.definitions(project)
    for name, spec in defs.items():
        mark = "*" if name == default else " "
        print(f"{mark} {name:<14} {spec.get('description', '')}")
    return 0


def cmd_loop_run(args) -> int:
    from sagent.core import loops
    from sagent.core.errors import SagentError

    _bootstrap(args)
    task = args.task if args.task != "-" else sys.stdin.read()
    try:
        lr = loops.start(_actor(args), _resolve_slug(args), args.name, task, provider=args.agent)
    except SagentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"loop #{lr.id} {lr.loop_name} {lr.status} · node {lr.current_node}")
    if not args.wait:
        return 0
    last = None
    while True:
        lr = loops.wait(lr.id, timeout=2, poll=0.5)
        state = (lr.status, lr.current_node, lr.iteration)
        if state != last:
            print(f"  {lr.status:<9} node={lr.current_node} iteration={lr.iteration}")
            last = state
        if lr.status != "RUNNING":
            break
    if lr.error:
        print(f"  error: {lr.error}")
    return 0 if lr.status == "SUCCESS" else 2


def cmd_test(args) -> int:
    from sagent import db as _db
    from sagent.core import runs, tests
    from sagent.core.errors import SagentError

    _bootstrap(args)
    try:
        run = tests.start_suite(_actor(args), _resolve_slug(args), args.suite)
    except SagentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.suite}: run #{run.id} started")
    run = runs.wait(run.id, timeout=args.timeout)
    row = _db.query_one("SELECT * FROM test_runs WHERE run_id = ?", (run.id,))
    if row:
        print(f"{row['status']}: total={row['total']} passed={row['passed']} failed={row['failed']} "
              f"skipped={row['skipped']}")
        for c in tests.cases(row["id"]):
            if c["status"] in ("failed", "flaky"):
                print(f"  ✗ {c['file']} › {c['title']}")
    return 0 if run.status == "SUCCESS" else 2


def cmd_review(args) -> int:
    from sagent import db as _db
    from sagent.core import reviews, runs
    from sagent.core.errors import SagentError

    _bootstrap(args)
    try:
        run = reviews.start(_actor(args), _resolve_slug(args), base_ref=args.base, provider=args.agent)
    except SagentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"review: run #{run.id} ({run.provider}) started")
    runs.wait(run.id, timeout=args.timeout)
    rr = _db.query_one("SELECT * FROM review_runs WHERE run_id = ?", (run.id,))
    print(f"{(rr['verdict'] or rr['status']).upper()}  critical={rr['critical']} high={rr['high']} "
          f"medium={rr['medium']} low={rr['low']}")
    if rr["summary"]:
        print(rr["summary"])
    for i in reviews.issues(rr["id"]):
        loc = f"{i['file']}:{i['line']}" if i["line"] else i["file"]
        print(f"  [{i['severity']}] {loc} — {i['reason']}")
    return 0 if rr["verdict"] == "approve" else 2


def cmd_usage(args) -> int:
    from sagent.core import projects, usage

    _bootstrap(args)
    actor = _actor(args)
    if args.project:
        project, _ = projects.get(actor, args.project, "usage.view")
        scope = {"project_id": project.id}
    elif actor.id is None:
        scope = {}
    else:
        scope = {"user_id": actor.id, "project_ids": usage.visible_project_ids(actor)}
    t = usage.totals(days=args.days, **scope)
    print(f"last {args.days} day(s): runs={t['runs']} tokens={t['tokens']:,} (in {t['input']:,} / out {t['output']:,}"
          f" / cache {t['cache_read'] + t['cache_write']:,}) cost=${t['cost']:.4f} agent_time={t['agent_ms'] // 60000}m")
    for dim in ("user", "project", "model"):
        rows = usage.breakdown(dim, days=args.days, **scope)
        if rows:
            print(f"by {dim}:")
            for r in rows:
                print(f"  {str(r['label'])[:30]:<30} runs={r['runs']:<4} tokens={r['tokens']:>12,} cost=${r['cost']:.4f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sagent", description="Control plane for CLI coding agents")
    ap.add_argument("--home", help="data directory (default: $SAGENT_HOME or ~/.sagent)")
    ap.add_argument("--version", action="version", version=f"sagent {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    web = sub.add_parser("web", help="run the web UI")
    web.add_argument("--host")
    web.add_argument("--port", type=int)
    web.add_argument("--debug", action="store_true")
    web.add_argument("--waitress", action="store_true", help="serve with waitress (pip install sagent[serve])")
    web.set_defaults(func=cmd_web)

    user = sub.add_parser("user", help="manage users").add_subparsers(dest="user_cmd", required=True)
    for name, role in (("create-admin", "admin"), ("create", "member")):
        p = user.add_parser(name)
        p.add_argument("--username")
        p.add_argument("--display-name")
        p.add_argument("--password-stdin", action="store_true")
        p.set_defaults(func=lambda a, r=role: cmd_user_create(a, r))
    p = user.add_parser("list")
    p.set_defaults(func=cmd_user_list)
    p = user.add_parser("reset-password")
    p.add_argument("username")
    p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(func=cmd_user_reset)

    su = sub.add_parser("setup-url", help="print the one-time link that creates the first admin")
    su.add_argument("--host")
    su.add_argument("--port", type=int)
    su.set_defaults(func=cmd_setup_url)

    doc = sub.add_parser("doctor", help="check the local environment")
    doc.set_defaults(func=cmd_doctor)

    proj = sub.add_parser("project", help="projects").add_subparsers(dest="project_cmd", required=True)
    p = proj.add_parser("list")
    p.add_argument("--as", dest="as_user")
    p.add_argument("--all", action="store_true", help="include archived")
    p.set_defaults(func=cmd_project_list)

    r = sub.add_parser("run", help="start an agent run (or a project command)")
    r.add_argument("prompt", nargs="?", default="", help="task text, or - to read stdin")
    r.add_argument("--project", help="project slug (default: project of the current directory)")
    r.add_argument("--agent", choices=("claude", "codex"))
    r.add_argument("--interactive", action="store_true", help="interactive session, attach immediately")
    r.add_argument("--resume", help="agent session id to continue")
    r.add_argument("--command", help="run a shell command instead of an agent")
    r.add_argument("--attach", action="store_true")
    r.add_argument("--wait", action="store_true", help="wait for completion and print the result")
    r.add_argument("--timeout", type=float)
    r.add_argument("--as", dest="as_user")
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="list runs of a project, or show one run")
    st.add_argument("run_id", nargs="?", type=int)
    st.add_argument("--project")
    st.add_argument("--limit", type=int, default=20)
    st.add_argument("--as", dest="as_user")
    st.set_defaults(func=cmd_status)

    at = sub.add_parser("attach", help="attach to a run's tmux session")
    at.add_argument("run_id", type=int)
    at.add_argument("--as", dest="as_user")
    at.set_defaults(func=cmd_attach)

    sp = sub.add_parser("stop", help="stop a run")
    sp.add_argument("run_id", type=int)
    sp.add_argument("--as", dest="as_user")
    sp.set_defaults(func=cmd_stop)

    lp = sub.add_parser("loop", help="development loops").add_subparsers(dest="loop_cmd", required=True)
    p = lp.add_parser("list")
    p.add_argument("--project")
    p.add_argument("--as", dest="as_user")
    p.set_defaults(func=cmd_loop_list)
    p = lp.add_parser("run")
    p.add_argument("name", help="loop name from .sagent/loops.yaml ('' = default)")
    p.add_argument("task", help="task text, or - to read stdin")
    p.add_argument("--project")
    p.add_argument("--agent", choices=("claude", "codex"))
    p.add_argument("--wait", action="store_true")
    p.add_argument("--as", dest="as_user")
    p.set_defaults(func=cmd_loop_run)

    ts = sub.add_parser("test", help="run a test suite from .sagent/tests.yaml")
    ts.add_argument("suite", nargs="?", default="unit", choices=("unit", "lint", "typecheck", "e2e"))
    ts.add_argument("--project")
    ts.add_argument("--timeout", type=float)
    ts.add_argument("--as", dest="as_user")
    ts.set_defaults(func=cmd_test)

    rv = sub.add_parser("review", help="AI review of the working tree vs a git ref")
    rv.add_argument("--base", default="HEAD")
    rv.add_argument("--agent", choices=("claude", "codex"))
    rv.add_argument("--project")
    rv.add_argument("--timeout", type=float)
    rv.add_argument("--as", dest="as_user")
    rv.set_defaults(func=cmd_review)

    us = sub.add_parser("usage", help="token / cost usage")
    us.add_argument("--project")
    us.add_argument("--days", type=int, default=7, choices=(1, 7, 30, 90))
    us.add_argument("--as", dest="as_user")
    us.set_defaults(func=cmd_usage)

    sc = sub.add_parser("scan", help="analyse a project directory (read-only)")
    sc.add_argument("path", nargs="?", default=".")
    sc.set_defaults(func=cmd_scan)

    for name, default_path, helptext in (("import", None, "register an existing project and bootstrap .sagent/"),
                                         ("init", ".", "bootstrap the current directory")):
        p = sub.add_parser(name, help=helptext)
        if default_path is None:
            p.add_argument("path")
        else:
            p.set_defaults(path=default_path)
        p.add_argument("--name")
        p.add_argument("--agent", choices=("claude", "codex"))
        p.add_argument("--review-agent", choices=("claude", "codex", "same"))
        p.add_argument("--loop")
        p.add_argument("--as", dest="as_user", help="owner username (default: local operator)")
        p.add_argument("--create", action="store_true", help="create the directory if missing")
        p.add_argument("--overwrite", action="store_true", help="overwrite existing .sagent files")
        p.add_argument("-y", "--yes", action="store_true", help="accept defaults without prompting")
        p.set_defaults(func=cmd_import)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
