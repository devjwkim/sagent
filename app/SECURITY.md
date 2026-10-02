# Security

## Deployment model

- By default sagent binds to `127.0.0.1` only.
- If you expose it to other machines, put it behind an HTTPS reverse proxy and set `SAGENT_TRUST_PROXY=1` and `SAGENT_SECURE_COOKIES=1`.
- `--debug` is refused unless sagent is bound to localhost.

## Trust boundary — read this before inviting anyone

- Coding agents run as the OS account that runs sagent, with that account's HOME and its Claude Code / Codex logins.
- **A user who can start a run (project role `developer` or higher) effectively has a shell as that account.** In practice, such a user can:
  - read `SAGENT_HOME` (the session-signing secret, the keystore and the database), which lets them impersonate any sagent user, including admins;
  - reach other projects' files and tmux sessions;
  - edit `.sagent/*.yaml` directly, which bypasses the maintainer-only harness permissions.
- Treat `developer` as "trusted operator of this server". The per-project roles organise collaboration and attribution between people who already trust each other. They are **not** a security boundary against a malicious developer.
- Agent permission settings in `.sagent/harness.yaml` are mapped to the agent CLI's own flags: `--permission-mode` and `--allowedTools` for Claude, `--sandbox` for Codex. They are policy, not a sandbox.
- Members cannot create projects unless an admin enables it (`projects.member_can_create`, off by default) and configures allowed directories. Project paths may not be nested in each other.
- To separate agents from the sagent account, set **Server settings → command prefix**. It is a JSON argv that every agent and test process is started through. Examples:
  - `["sudo", "-n", "-u", "sagent-agent", "--"]`: a separate OS account. Give it its own agent logins and no read access to `SAGENT_HOME`. The project directories must be writable by it.
  - `["bwrap", "--dev-bind", "/", "/", "--tmpfs", "/home/you/.sagent", "--"]`: hides the sagent data directory.
  - a `docker run …` wrapper.

  sagent does not ship or verify the sandbox itself; test your prefix with `sagent run --command "ls ~/.sagent"`. Without a prefix, run one sagent instance per trusted team.
- Members can only register projects inside directories an admin allows. Paths are resolved with `realpath` before checking, so symlinks cannot be used to escape.

## What sagent does

- **Passwords:** hashed with scrypt.
- **Login:** lockout by IP and by username. Error messages are the same for an unknown user and a wrong password.
- **Sessions:** the `sagent_session` cookie is HttpOnly and SameSite=Lax, and is Secure behind HTTPS. Sessions are revoked when a password, role or active flag changes.
- **CSRF:** a per-session CSRF token is required on every state-changing request.
- **Browser hardening:** strict Content-Security-Policy with no inline scripts, plus `X-Frame-Options: DENY` and `nosniff`.
- **Project isolation:** projects you are not a member of are hidden and return 404.
- **Audit log:** every mutation is recorded. Passwords and tokens are never logged.
- **Stored secrets:** secrets in the database (for example OTLP headers) are encrypted with a Fernet key kept in `SAGENT_HOME/keystore.key` (mode 0600). OTel settings are only injected into agent processes if an admin enables it, and the OTLP credential needs a separate opt-in, because anything in an agent's environment is readable by the person driving it.
- **Login:** lockout per IP; per username at 3× the threshold, to slow distributed guessing without letting anyone lock a known account out with a few attempts. Password changes count toward the lockout. Behind a proxy with `SAGENT_TRUST_PROXY=1`, bind sagent to `127.0.0.1` so `X-Forwarded-For` cannot be spoofed by connecting directly.
- **No shell interpolation:** subprocesses are started with argument lists, never with `shell=True`. tmux only ever receives a fixed command line; the real command, environment and prompts travel through files in the run directory, because tmux interprets `;` in its own arguments.
- **Web terminal:** the WebSocket endpoint checks `Origin` (WebSocket upgrades are not covered by CSRF tokens). Users without input permission are attached read-only (`tmux attach -r`). Access is re-checked every few seconds, so revoked users are dropped. The number of typed characters is audited; the text itself is not. The private tmux server runs without a config file and with **no key bindings** (no prefix key, no mouse), so a terminal user cannot reach tmux commands to switch to another session.
- **Option injection:** session ids must start with an alphanumeric character, and prompts are passed after `--`, so user input is never parsed as a CLI flag.
- **git:** repository-local config that can execute commands (`core.fsmonitor`, hooks, external diff) is disabled for every git call sagent makes. Only the run page relaxes `style-src` to `'unsafe-inline'`, which xterm.js needs; `script-src` stays `'self'`.
- **Test artifacts:** Playwright reports, screenshots and attachments are produced by the project under test and are treated as untrusted. They are served with a CSP `sandbox`, so they run in an opaque origin and cannot read sagent cookies.
- **API tokens:** the JSON API accepts only bearer tokens, never the session cookie, so it needs no CSRF token and cannot be driven from another site. Only a SHA-256 hash of each token is stored. Tokens expire (90 days by default) and can be revoked. Failed token attempts count toward the IP lockout.
- **Log retention:** run directories (terminal logs, prompts, agent output) are deleted after `runs.log_retention_days` (default 30). Usage statistics are kept.

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository instead of a public issue.
