# Changelog

## 0.1.0.dev0 (unreleased)

First public version.

- **Multi-user:**
  - Accounts and global roles (admin / member).
  - Per-project roles: viewer, developer, maintainer, owner.
  - 404 isolation for projects you are not a member of.
  - Audit log, CSRF protection, strict CSP and login lockout.
- **Projects:**
  - Project registry and a read-only bootstrap scanner.
  - `.sagent/` harness: project, harness, loops, tests and review YAML.
  - `CLAUDE.md` / `AGENTS.md` generated once if missing.
- **Agents:**
  - Claude Code and Codex CLI adapters running in a private tmux server, with a normalised event store.
  - Headless and interactive runs.
  - The harness permission policy is mapped to agent CLI flags.
  - Checked against Claude Code 2.1 and codex-cli 0.160.
- **Loops:**
  - YAML graph engine: conditional edges, retries, timeouts and max iterations.
  - Controls: pause, resume, retry, skip, rerun from a node, and Take Control / return to automation.
  - SVG runtime view, visual editor and shared template library.
- **QA:**
  - Unit, lint, typecheck and Playwright suites, with JUnit and Playwright result parsing.
  - Sandboxed HTML reports and AI failure analysis.
  - E2E scenario wizard and snapshot baseline updates.
- **AI PR review:**
  - The reviewer gets the diff, the latest test results and the project rules, and runs read-only.
  - It returns a JSON verdict with severity-ranked issues; `block_on` controls which severities reject.
- **LLMOps:**
  - Usage and cost per user, project, agent and model.
  - Usage read from Claude and Codex session transcripts.
  - Editable price table and quality metrics.
  - Versioned prompt templates and OpenTelemetry export.
- **Web terminal:** xterm.js over WebSocket into tmux, with an Origin check, read-only attach and live permission re-checks.
- **API:** personal API tokens and a JSON API (`/api/v1`).
- **Security:** an optional command prefix to run agents under another account or in a sandbox. See [SECURITY.md](SECURITY.md).
