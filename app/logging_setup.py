"""Logging with a last-line-of-defence redaction filter (tokens, JWTs, emails, UUIDs)."""

from __future__ import annotations

import logging
import re
import sys

from opentelemetry import trace

PATTERNS = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), "[jwt]"),
    (re.compile(r"(?i)(access_token|refresh_token|code|token|password|client_secret)=[^&\s]+"), r"\1=[redacted]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "[uuid]"),
    (re.compile(r"\b\d{18}\b"), "[meter-id]"),  # EAN-18 metering point ids
]


def redact(text: str) -> str:
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else "-"
        return True


def setup_logging() -> logging.Logger:
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler(sys.stdout)
        handler.addFilter(RedactingFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s trace=%(trace_id)s %(message)s"))
        root.handlers = [handler]
        root.setLevel(logging.INFO)
        # httpx logs full URLs at INFO (which can include OAuth codes); keep it quiet.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)
    return logging.getLogger("home_energy")
