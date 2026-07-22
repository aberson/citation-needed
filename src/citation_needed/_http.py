"""Shared HTTP constants — the ONE source of truth for client shape defaults.

Both resolve.py (structured-API clients) and verify.py (the SSRF-guarded fetch seam)
import :data:`DEFAULT_TIMEOUT` from here instead of declaring their own copies
(code-quality.md § one source of truth for data-shape constants). A regression test
asserts identity (``is``, not ``==``) so a future re-duplication fails CI.
"""

from __future__ import annotations

import httpx

#: Connect + read (+ write/pool) timeouts on every request; injected clients carry their own.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

__all__ = ["DEFAULT_TIMEOUT"]
