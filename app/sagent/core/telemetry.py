"""OpenTelemetry export (optional).

Local SQLite is always the source of truth. When an OTLP endpoint is set
(admin setting `otel.endpoint` or env OTEL_EXPORTER_OTLP_ENDPOINT) and the
`otel` extra is installed, every finished run and loop run is exported as a
span with the PRD attributes. If the project's harness enables
`telemetry.otel`, Claude Code processes also get the env vars that make the
CLI export its own metrics/logs to the same endpoint — no per-user setup.
"""
from __future__ import annotations

import os
from datetime import datetime

from sagent import db
from sagent.core import harness, projects, runs, settings, users

_provider = None
_tracer = None
_configured_for: tuple | None = None


def endpoint() -> str:
    return (settings.get("otel.endpoint") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")).strip()


def headers() -> str:
    return settings.get("otel.headers") or os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")


def available() -> bool:
    try:
        import opentelemetry.sdk.trace  # noqa: F401
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: F401
    except ImportError:
        return False
    return True


def _parse_headers(raw: str) -> dict[str, str]:
    out = {}
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def tracer(exporter=None):
    """Return a tracer, (re)configured when the endpoint changes. `exporter`
    lets tests inject an in-memory exporter."""
    global _provider, _tracer, _configured_for
    ep = endpoint()
    key = (ep, headers(), id(exporter) if exporter else None)
    if _tracer is not None and _configured_for == key:
        return _tracer
    if exporter is None and (not ep or not available()):
        return None
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        url = ep.rstrip("/")
        if not url.endswith("/v1/traces"):
            url += "/v1/traces"
        exporter = OTLPSpanExporter(endpoint=url, headers=_parse_headers(headers()))
        processor = BatchSpanProcessor(exporter)
    else:
        processor = SimpleSpanProcessor(exporter)
    if _provider is not None:
        _provider.shutdown()
    from sagent import __version__

    _provider = TracerProvider(resource=Resource.create({"service.name": "sagent", "service.version": __version__}))
    _provider.add_span_processor(processor)
    _tracer = _provider.get_tracer("sagent")
    _configured_for = key
    return _tracer


def _ns(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso).timestamp() * 1_000_000_000)


def export_run(run_id: int) -> None:
    t = tracer()
    if t is None:
        return
    run = runs._load(run_id)
    if run.is_active:
        return
    slug = db.scalar("SELECT slug FROM projects WHERE id = ?", (run.project_id,)) or ""
    username = db.scalar("SELECT username FROM users WHERE id = ?", (run.created_by,)) or "system"
    from opentelemetry.trace import Status, StatusCode

    attrs = {
        "user.id": username, "project.id": slug, "run.id": run.id, "run.kind": run.kind,
        "loop.id": run.loop_run_id or 0, "node.id": run.node_key or "",
        "agent.provider": run.provider, "agent.session_id": run.agent_session_id or "",
        "model": run.model or "", "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
        "cache_read_tokens": run.cache_read_tokens, "cache_write_tokens": run.cache_write_tokens,
        "cost_usd": float(run.cost_usd or 0), "duration_ms": run.duration_ms or 0, "status": run.status,
        "prompt.template": run.prompt_template or "", "prompt.version": run.prompt_version or 0,
    }
    span = t.start_span(f"sagent.run.{run.kind}", start_time=_ns(run.started_at or run.created_at), attributes=attrs)
    if run.status != "SUCCESS":
        span.set_status(Status(StatusCode.ERROR, run.error[:200]))
    span.end(end_time=_ns(run.finished_at) or None)


def _on_event(ev: dict) -> None:
    if ev.get("type") in ("run.completed", "run.failed", "run.cancelled") and ev.get("run_id"):
        export_run(ev["run_id"])


def agent_env(run, project) -> dict[str, str]:
    """Env for Claude Code so the CLI itself exports metrics/logs (opt-in per project)."""
    ep = endpoint()
    if not ep or run.provider != "claude" or run.kind != "agent" or not settings.get_bool("otel.agent_env"):
        return {}
    cfg = harness.load(project)["harness.yaml"]
    if not (cfg.get("telemetry") or {}).get("otel"):
        return {}
    username = db.scalar("SELECT username FROM users WHERE id = ?", (run.created_by,)) or "system"
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": ep,
        "OTEL_RESOURCE_ATTRIBUTES": f"sagent.user={username},sagent.project={project.slug},sagent.run_id={run.id}",
    }
    # The OTLP credential is an org secret: anything in the agent env is
    # readable by whoever drives the agent, so share it only when the admin
    # explicitly allows it.
    if headers() and settings.get_bool("otel.agent_headers"):
        env["OTEL_EXPORTER_OTLP_HEADERS"] = headers()
    return env


runs.add_listener(_on_event)
runs.add_env_provider(agent_env)
