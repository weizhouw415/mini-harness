"""Remove credential-like values from text sent to memory jobs."""

import re


_INLINE_SECRET = re.compile(
    r"(?i)((?:authorization|[\w-]*(?:password|secret|api[-_]?key|token))\s*['\"]?\s*[:=]\s*"
    r"|\b(?:bearer|basic)\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_CREDENTIAL_URL = re.compile(r"(?<=://)[^\s/@]+:[^\s/@]+@")
_LONG_OPAQUE = re.compile(r"[A-Za-z0-9+/_=-]{120,}")


def _safe_text(text: str, limit: int = 2000) -> str:
    text = text[:limit]
    text = _INLINE_SECRET.sub(r"\1[omitted]", text)
    text = _CREDENTIAL_URL.sub("[omitted]@", text)
    return _LONG_OPAQUE.sub("[omitted]", text)
