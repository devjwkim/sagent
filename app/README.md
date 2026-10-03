# sagent

**A multi-user control plane for CLI coding agents.**

sagent does not ship its own LLM or coding agent. It runs the agents you already use, [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [OpenAI Codex CLI](https://github.com/openai/codex), inside `tmux`, and manages the work around them for a whole team:

- **Harness**: per-project rules, context, test/review policy and agent permissions, kept in `.sagent/` inside the project repository.
- **Loops**: repeatable Plan → Implement → Test → Review → Fix workflows, defined as YAML graphs with conditional edges, retries and live status.
- **Runs and terminals**: start, watch and stop agent sessions, and take them over in a real terminal in the browser (xterm.js over WebSocket into the `tmux` session). Loops can be paused, handed to a human (Take Control), and returned to automation.
- **Visual loop editor**: drag nodes, connect them and set conditions; saved back to `.sagent/loops.yaml`. Loops can be shared through an organisation-wide template library.
- **QA**: unit / lint / typecheck commands and Playwright E2E with per-run HTML reports, screenshots and traces, AI failure analysis, an E2E scenario wizard that asks the agent to write Playwright tests for detected pages, and visual-regression baseline updates.
- **AI review**: structured PR review that can use a different agent than the one that wrote the code.
- **LLMOps**: token, time and cost usage per user, project, agent and model; run traces; prompt template versions; loop success and retry metrics; optional OpenTelemetry export.
- **Multi-user**: accounts, global roles, per-project roles (viewer / developer / maintainer / owner), project isolation and an audit log.

> Status: early development. The v0.1 MVP and most v0.2 items work end to end; expect breaking changes.

## Requirements

- Python 3.11+
- `tmux` and `git`
- At least one agent CLI, installed and already logged in on the server account: `claude` and/or `codex`
- Optional: Node.js + Playwright for E2E tests

## Install

**On a server, from a git checkout** (recommended). Everything stays in the checkout folder:

```bash
git clone https://github.com/devjwkim/sagent.git /data/sagent
bash /data/sagent/app/scripts/install.sh --host 0.0.0.0     # omit --host to allow only local access
# → virtualenv /data/sagent/.venv (editable), data /data/sagent/data, command ~/.local/bin/sagent
# update later:
cd /data/sagent && git pull && bash app/scripts/install.sh --upgrade
```

**One-line installer** (Linux / macOS, without a checkout):

```bash
curl -fsSL https://raw.githubusercontent.com/devjwkim/sagent/main/app/scripts/install.sh | bash
```

The installer:
- creates its own virtualenv (in the checkout, or `~/.local/share/sagent`) and installs a `sagent` command in `~/.local/bin` that always uses the right data folder;
- **starts the server** on port 17832, as a systemd user service when available or as a background process otherwise;
- prints a **one-time setup link**. Open it in a browser to create the first administrator.

Everything after that is done in the web UI.

```bash
# options: --host 0.0.0.0 (LAN access), --port N, --no-start, --no-service, --extras otel,
#          --upgrade, --uninstall [--purge]
bash install.sh --help
sagent setup-url        # show the setup link again (until the first admin exists)
```

**pipx**

```bash
pipx install "git+https://github.com/devjwkim/sagent.git#subdirectory=app"
```

**Wheel** (offline machines). Build it once, then copy the file:

```bash
cd app && python -m build --wheel          # → dist/sagent-<version>-py3-none-any.whl
pip install dist/sagent-*.whl              # or: bash scripts/install.sh --source dist/sagent-*.whl
```

**From a checkout** (development): `cd app && pip install -e ".[dev]"`.

## Quick start

```bash
sagent doctor               # check tmux / git / claude / codex / playwright
sagent web                  # http://127.0.0.1:17832 — prints a one-time setup link on first run
# or create the first admin in a terminal instead: sagent user create-admin
```

With `install.sh --service`, `sagent web` runs as a systemd user service (`systemctl --user status sagent`). To keep it running after you log out, enable lingering for your account: `sudo loginctl enable-linger $USER`.

`sagent web` uses the built-in threaded server, which also carries the WebSocket terminal. `--waitress` serves the pages with waitress but without the interactive terminal; terminal snapshots still update by polling.

Data lives in `~/.sagent/` (override with `SAGENT_HOME`). The SQLite database, the session secret and the keystore key are created there on first run with owner-only permissions.

## CLI

```text
sagent web                     run the web UI
sagent doctor                  check tmux / git / agents / playwright / OTel
sagent user create-admin|create|list|reset-password
sagent import PATH | init      register a project and bootstrap .sagent/
sagent scan [PATH]             read-only project analysis
sagent run "task" [--agent codex] [--interactive] [--wait]
sagent status [RUN] | attach RUN | stop RUN
sagent loop list | loop run NAME "task" [--wait]
sagent test [unit|lint|typecheck|e2e]
sagent review [--base REF] [--agent X]
sagent usage [--project SLUG] [--days 7]
```

The CLI uses the same core as the web UI. It runs on the server with direct database access, so it acts as the local operator unless `--as USER` is given.

## JSON API

Create a personal token under **API 토큰** (account menu). A token is shown once and acts with your project permissions. `read` tokens can only call GET endpoints.

```bash
curl -H "Authorization: Bearer $SAGENT_TOKEN" http://127.0.0.1:17832/api/v1/projects
curl -X POST -H "Authorization: Bearer $SAGENT_TOKEN" -H "Content-Type: application/json" \
     -d '{"loop": "standard", "task": "Add rate limiting to /login"}' \
     http://127.0.0.1:17832/api/v1/projects/my-app/loops
```

| Method | Path |
|--------|------|
| GET | `/api/v1/me`, `/api/v1/projects`, `/api/v1/usage?project=&days=` |
| GET / POST | `/api/v1/projects/<slug>/runs` (POST: `{prompt, provider?}`) |
| GET | `/api/v1/runs/<id>`, `/api/v1/loops/<id>`, `/api/v1/reviews/by-run/<run_id>` |
| POST | `/api/v1/projects/<slug>/loops` `{loop, task}`, `/tests` `{suite}`, `/reviews` `{base_ref}` |
| POST | `/api/v1/runs/<id>/stop`, `/api/v1/loops/<id>/stop` |

## Project files

```text
my-project/
  .sagent/
    project.yaml   name, detected stack
    harness.yaml   rules, forbidden actions, context files, agents per role, permissions, telemetry
    loops.yaml     loop graphs (nodes, conditional edges, retries, timeouts, max iterations)
    tests.yaml     unit / lint / typecheck / e2e commands
    review.yaml    reviewer agent, inputs, severities that block
  CLAUDE.md / AGENTS.md   generated once if missing
```

`.sagent/` is meant to be committed. It never contains secrets.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `SAGENT_HOME` | `~/.sagent` | data directory |
| `SAGENT_HOST` / `SAGENT_PORT` | `127.0.0.1` / `17832` | bind address |
| `SAGENT_TRUST_PROXY` | off | trust `X-Forwarded-*` from one reverse proxy |
| `SAGENT_SECURE_COOKIES` | off | mark cookies `Secure` (enable behind HTTPS) |
| `SAGENT_SECRET_KEY` | generated | override the session signing key |

Runtime settings, such as the directories members may register projects under and the login lockout policy, are edited by admins in the web UI.

## Permission model

| Project role | Can |
|--------------|-----|
| viewer | see the project, runs, test/review results and usage |
| developer | also start runs, see terminals, and control or type into **their own** runs |
| maintainer | also edit harness, loops and prompts, change project settings, and control any run |
| owner | also manage members and archive the project |

Global `admin`s manage users and server settings and act as owner on every project. Projects you are not a member of return 404.

**Trust boundary:** agents run as the OS user that runs `sagent web`, with that account's agent logins. A developer, and anyone with a higher role, can make an agent do anything that account can do. That includes reading sagent's own secrets. So treat developer and above as trusted operators of the server. Roles organise a team that already trusts each other; they do not isolate a malicious user. See [SECURITY.md](SECURITY.md).

## Development

```bash
pip install -e ".[dev,otel]"
python -m playwright install chromium   # browser tests (optional; skipped if missing)
pytest
python scripts/check_secrets.py   # pre-publish scan for secrets / private infra details
```

## License

MIT
