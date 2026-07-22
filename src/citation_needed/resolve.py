"""Structured-API citation resolution — Semantic Scholar, Crossref, OpenAlex clients.

The three REST/JSON APIs are called directly by this module's own httpx code, **never**
through any LLM or the harness WebFetch tool (WebFetch summarizes through a model — wrong
tool for JSON; docs/research/citation-mechanics.md §d). Every hit carries the API's own
JSON echo (:attr:`PaperHit.raw`) so the anti-fabrication gate (verify.py) can store a
resolution record captured from the actual response, not an LLM's narration of it.

Backend contract (plan.md §4.3):

- **Semantic Scholar Graph** — keyless default first search; optional free key via the
  ``CITATION_NEEDED_S2_KEY`` env var (sent as ``x-api-key``). 429s get a bounded,
  logged backoff-and-retry honoring ``Retry-After`` when present, then raise.
- **Crossref REST** — DOI canonicalization + bibliographic search, ``mailto`` param for
  the polite pool (placeholder constant, operator-overridable via
  ``CITATION_NEEDED_CROSSREF_MAILTO``). Throttled from LIVE response headers ONLY:
  each response's ``X-Rate-Limit-Limit`` / ``X-Rate-Limit-Interval`` pace subsequent
  calls (:class:`CrossrefThrottle`); a header-absent response falls back to a
  conservative 1 req/s — that number is the documented header-absent fallback, never a
  hardcoded claim about the API's limit (confirmed source conflict on static numbers).
- **OpenAlex** — broad-coverage fallback on a Semantic Scholar miss. A free API key is
  REQUIRED since 2026-02-13 (``CITATION_NEEDED_OPENALEX_KEY``); when the key is unset
  and a lookup actually REACHES this tier, :class:`OpenAlexKeyMissing` raises loudly
  with the signup URL — never a silent skip.

All clients accept an injectable ``httpx.Client`` (tests pass ``httpx.MockTransport``
-backed clients); the default client carries connect+read timeouts on every request.
Failures are never hidden by retries: bounded, logged, then raised as
:class:`ResolutionError`.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from citation_needed._http import DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_KEY_ENV = "CITATION_NEEDED_S2_KEY"
S2_FIELDS = "title,year,authors,externalIds,url,abstract"

CROSSREF_WORKS_URL = "https://api.crossref.org/works"
CROSSREF_MAILTO_ENV = "CITATION_NEEDED_CROSSREF_MAILTO"
#: Placeholder polite-pool contact — the operator overrides via CROSSREF_MAILTO_ENV.
DEFAULT_CROSSREF_MAILTO = "citation-needed-operator@example.com"

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_KEY_ENV = "CITATION_NEEDED_OPENALEX_KEY"
OPENALEX_SIGNUP_URL = "https://openalex.org/"

USER_AGENT = "citation-needed/0.1 (citation trail for LLM-facing files)"

#: Bounded 429 handling for Semantic Scholar: total attempts, never an unbounded retry loop.
S2_MAX_ATTEMPTS = 4

#: Upper clamp (seconds) on any server-supplied Retry-After delay — a hostile or buggy
#: header must never hang the process; anything above this waits only the cap.
RETRY_AFTER_CAP_S = 120.0

#: Maximum abstract-snippet length carried on a hit (the full echo stays in ``raw``).
_SNIPPET_CHARS = 300

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)

_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*s\s*$", re.IGNORECASE)


class ResolutionError(RuntimeError):
    """A structured-API lookup failed (HTTP error, bad payload, retries exhausted)."""


class OpenAlexKeyMissing(ResolutionError):
    """A lookup REACHED the OpenAlex tier but ``CITATION_NEEDED_OPENALEX_KEY`` is unset."""

    def __init__(self) -> None:
        super().__init__(
            "the lookup reached the OpenAlex tier, but no API key is configured. "
            f"OpenAlex requires a (free) key since 2026-02-13: set the {OPENALEX_KEY_ENV} "
            f"environment variable (sign up at {OPENALEX_SIGNUP_URL}). "
            "This tier is never silently skipped."
        )


def normalize_doi(doi: str) -> str:
    """Canonical DOI form for identity/dedup: lowercase, resolver-prefix stripped.

    ``https://doi.org/10.48550/arXiv.2307.03172`` -> ``10.48550/arxiv.2307.03172``.
    The ONE normalization both resolution and the citations natural_key use
    (code-quality.md § one source of truth).
    """
    normalized = doi.strip().lower()
    for prefix in _DOI_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized.strip().strip("/")


@dataclass(frozen=True)
class PaperHit:
    """One candidate paper from one backend, carrying the API's own JSON echo."""

    source: str  # 'semantic_scholar' | 'crossref' | 'openalex'
    title: str
    year: int | None
    authors: tuple[str, ...]
    doi: str | None  # normalized (normalize_doi)
    arxiv_id: str | None
    url: str | None
    abstract_snippet: str | None
    raw: dict[str, Any]  # the API's own JSON for this hit — the resolution record


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of the tiered :func:`resolve_citation` orchestrator.

    ``resolved=False`` (no hit anywhere) is a first-class, legitimate outcome — the
    caller records the choice as no-literature-found and inserts nothing. ``notes``
    carries any per-tier failures that were degraded rather than raised (e.g. a
    DataCite/arXiv DOI that Crossref does not register).
    """

    query: str
    resolved: bool
    tier: str | None  # tier that produced the hit: 'semantic_scholar' | 'openalex'
    hit: PaperHit | None
    crossref_echo: dict[str, Any] | None  # Crossref JSON when the DOI canonicalized
    tiers_tried: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def resolution_record(self) -> dict[str, Any]:
        """The insert-time record: the actual API JSON echo(es), never LLM-narrated."""
        return {
            "query": self.query,
            "tier": self.tier,
            "api_echo": self.hit.raw if self.hit is not None else None,
            "crossref_echo": self.crossref_echo,
            "tiers_tried": list(self.tiers_tried),
            "notes": list(self.notes),
        }


class CrossrefThrottle:
    """Header-driven pacing for Crossref calls — never a hardcoded rate number.

    After every response, :meth:`observe` reads ``X-Rate-Limit-Limit`` (requests) and
    ``X-Rate-Limit-Interval`` (e.g. ``"1s"``) and sets the minimum spacing between calls
    to ``interval / limit`` seconds; :meth:`wait` (called before each request) sleeps
    whatever remains of that spacing. When a response carries no (or malformed) headers,
    pacing falls back to :data:`FALLBACK_MIN_INTERVAL_S` (1 req/s) — a conservative
    header-absent fallback, NOT a claim about the API's actual limit.

    Thread-safe: a :class:`threading.Lock` guards the pacing state, so concurrent
    callers sharing one throttle (e.g. a future sweep fan-out sharing
    ``_default_throttle``) serialize through :meth:`wait` and cannot both pass the
    spacing check before either records its call.

    ``clock``/``sleep`` are injectable for deterministic tests.
    """

    FALLBACK_MIN_INTERVAL_S = 1.0

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_call: float | None = None
        self.min_interval_s = self.FALLBACK_MIN_INTERVAL_S
        self.header_derived = False
        self.observed_count = 0

    def wait(self) -> None:
        """Block until the spacing derived from the LAST observed response has elapsed.

        The lock is deliberately held across the sleep: pacing IS serialization — a
        second caller must not run its own spacing check until the first has recorded
        its call time.
        """
        with self._lock:
            now = self._clock()
            if self._last_call is not None:
                delay = self._last_call + self.min_interval_s - now
                if delay > 0:
                    self._sleep(delay)
                    now += delay
            self._last_call = now

    def observe(self, response: httpx.Response) -> None:
        """Update pacing from one live response's rate-limit headers."""
        with self._lock:
            self.observed_count += 1
            limit_raw = response.headers.get("X-Rate-Limit-Limit")
            interval_raw = response.headers.get("X-Rate-Limit-Interval")
            if limit_raw is not None and interval_raw is not None:
                match = _INTERVAL_RE.match(interval_raw)
                try:
                    limit = int(limit_raw)
                except ValueError:
                    limit = 0
                if match is not None and limit > 0:
                    self.min_interval_s = int(match.group(1)) / limit
                    self.header_derived = True
                    return
                _LOGGER.warning(
                    "Crossref rate-limit headers malformed (limit=%r interval=%r); "
                    "falling back to %s s spacing",
                    limit_raw,
                    interval_raw,
                    self.FALLBACK_MIN_INTERVAL_S,
                )
            self.min_interval_s = self.FALLBACK_MIN_INTERVAL_S
            self.header_derived = False


#: Module-level shared throttle so every Crossref call in a process paces together.
_default_throttle = CrossrefThrottle()


def _build_client() -> httpx.Client:
    """Default production client (tests monkeypatch this to inject a MockTransport)."""
    return httpx.Client(timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT})


def _crossref_mailto() -> str:
    return os.environ.get(CROSSREF_MAILTO_ENV) or DEFAULT_CROSSREF_MAILTO


def _get_json(client: httpx.Client, url: str, params: dict[str, str]) -> Any:
    """One GET returning parsed JSON; transport/HTTP/parse failures raise, loudly."""
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise ResolutionError(f"request to {url} failed: {exc}") from exc
    if response.status_code != 200:
        raise ResolutionError(f"{url} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise ResolutionError(f"{url} returned non-JSON body") from exc


def _snippet(text: object) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    flat = " ".join(text.split())
    return flat[:_SNIPPET_CHARS]


def _parse_retry_after(value: str | None, fallback: float) -> float:
    """Defensive ``Retry-After`` parse — a hostile header must never crash or hang.

    Accepts the two RFC 9110 forms: integer seconds or an HTTP-date (delay = date minus
    now). The result is clamped to ``[0, RETRY_AFTER_CAP_S]`` — a negative value waits
    0 s, an absurdly large one waits only the cap. Anything unparseable (garbage text,
    malformed date) returns ``fallback`` (the caller's own backoff schedule).
    """
    if not value:
        return fallback
    try:
        seconds = float(int(value.strip()))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return fallback  # neither an integer nor an HTTP-date: garbage
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        seconds = (target - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), RETRY_AFTER_CAP_S)


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------


def _parse_s2_item(item: dict[str, Any]) -> PaperHit | None:
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    external_ids = item.get("externalIds")
    ids: dict[str, Any] = external_ids if isinstance(external_ids, dict) else {}
    doi_raw = ids.get("DOI")
    arxiv_raw = ids.get("ArXiv")
    year = item.get("year")
    authors_raw = item.get("authors")
    authors = tuple(
        str(author["name"])
        for author in (authors_raw if isinstance(authors_raw, list) else [])
        if isinstance(author, dict) and isinstance(author.get("name"), str)
    )
    url = item.get("url")
    return PaperHit(
        source="semantic_scholar",
        title=title,
        year=year if isinstance(year, int) else None,
        authors=authors,
        doi=normalize_doi(doi_raw) if isinstance(doi_raw, str) else None,
        arxiv_id=arxiv_raw if isinstance(arxiv_raw, str) else None,
        url=url if isinstance(url, str) else None,
        abstract_snippet=_snippet(item.get("abstract")),
        raw=item,
    )


def search_semantic_scholar(
    query: str,
    *,
    limit: int = 5,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[PaperHit]:
    """S2 Graph API paper relevance search (keyless default; optional env key).

    429 responses get a bounded backoff-and-retry (at most :data:`S2_MAX_ATTEMPTS`
    attempts total), honoring a ``Retry-After`` header when present — integer seconds
    or HTTP-date, defensively parsed and clamped to ``[0, RETRY_AFTER_CAP_S]`` — else
    exponential backoff (1, 2, 4 s) — each retry logged, then :class:`ResolutionError`.
    """
    owns_client = client is None
    active = client if client is not None else _build_client()
    headers = {"User-Agent": USER_AGENT}
    api_key = os.environ.get(S2_KEY_ENV)
    if api_key:
        headers["x-api-key"] = api_key
    params = {"query": query, "limit": str(limit), "fields": S2_FIELDS}
    try:
        for attempt in range(S2_MAX_ATTEMPTS):
            try:
                response = active.get(S2_SEARCH_URL, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise ResolutionError(f"Semantic Scholar request failed: {exc}") from exc
            if response.status_code == 429:
                if attempt == S2_MAX_ATTEMPTS - 1:
                    raise ResolutionError(
                        f"Semantic Scholar still rate-limiting after {S2_MAX_ATTEMPTS} attempts"
                    )
                delay = _parse_retry_after(
                    response.headers.get("Retry-After"), fallback=float(2**attempt)
                )
                _LOGGER.warning(
                    "Semantic Scholar 429 (attempt %d/%d); retrying in %.0f s",
                    attempt + 1,
                    S2_MAX_ATTEMPTS,
                    delay,
                )
                sleep(delay)
                continue
            if response.status_code != 200:
                raise ResolutionError(
                    f"Semantic Scholar search returned HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ResolutionError("Semantic Scholar returned non-JSON body") from exc
            data = payload.get("data") if isinstance(payload, dict) else None
            if data is None:
                return []  # zero-hit responses may omit `data` entirely
            if not isinstance(data, list):
                raise ResolutionError(
                    "Semantic Scholar response shape unexpected: 'data' not a list"
                )
            return [
                hit
                for item in data
                if isinstance(item, dict) and (hit := _parse_s2_item(item)) is not None
            ]
        raise AssertionError("unreachable: S2 retry loop always returns or raises")
    finally:
        if owns_client:
            active.close()


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------


def _crossref_year(message: dict[str, Any]) -> int | None:
    for key in ("issued", "published-print", "published-online", "created"):
        block = message.get(key)
        if isinstance(block, dict):
            parts = block.get("date-parts")
            if (
                isinstance(parts, list)
                and parts
                and isinstance(parts[0], list)
                and parts[0]
                and isinstance(parts[0][0], int)
            ):
                return parts[0][0]
    return None


def _parse_crossref_item(item: dict[str, Any]) -> PaperHit | None:
    titles = item.get("title")
    title = titles[0] if isinstance(titles, list) and titles else None
    if not isinstance(title, str) or not title.strip():
        return None
    doi_raw = item.get("DOI")
    authors_raw = item.get("author")
    authors = tuple(
        " ".join(part for part in (author.get("given"), author.get("family")) if part)
        for author in (authors_raw if isinstance(authors_raw, list) else [])
        if isinstance(author, dict) and (author.get("given") or author.get("family"))
    )
    url = item.get("URL")
    containers = item.get("container-title")
    return PaperHit(
        source="crossref",
        title=title,
        year=_crossref_year(item),
        authors=authors,
        doi=normalize_doi(doi_raw) if isinstance(doi_raw, str) else None,
        arxiv_id=None,
        url=url if isinstance(url, str) else None,
        abstract_snippet=_snippet(
            containers[0] if isinstance(containers, list) and containers else None
        ),
        raw=item,
    )


def _crossref_get(
    url: str,
    params: dict[str, str],
    client: httpx.Client | None,
    throttle: CrossrefThrottle | None,
) -> Any:
    """Throttled Crossref GET: wait per the last observed headers, call, observe."""
    active_throttle = throttle if throttle is not None else _default_throttle
    owns_client = client is None
    active = client if client is not None else _build_client()
    try:
        active_throttle.wait()
        try:
            response = active.get(url, params=params, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            raise ResolutionError(f"Crossref request failed: {exc}") from exc
        active_throttle.observe(response)
        if response.status_code != 200:
            raise ResolutionError(f"Crossref returned HTTP {response.status_code} for {url}")
        try:
            return response.json()
        except ValueError as exc:
            raise ResolutionError("Crossref returned non-JSON body") from exc
    finally:
        if owns_client:
            active.close()


def lookup_crossref_doi(
    doi: str,
    *,
    client: httpx.Client | None = None,
    throttle: CrossrefThrottle | None = None,
) -> dict[str, Any]:
    """Canonicalize one DOI via ``/works/{doi}``; returns the Crossref ``message`` echo.

    Raises :class:`ResolutionError` on any non-200 — including 404 for DOIs Crossref
    does not register (e.g. DataCite-registered arXiv DOIs); the caller decides whether
    that is fatal.
    """
    normalized = normalize_doi(doi)
    if not normalized:
        raise ResolutionError(f"cannot canonicalize an empty DOI ({doi!r})")
    payload = _crossref_get(
        f"{CROSSREF_WORKS_URL}/{normalized}", {"mailto": _crossref_mailto()}, client, throttle
    )
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        raise ResolutionError("Crossref DOI response shape unexpected: no 'message' object")
    return message


def search_crossref(
    query: str,
    *,
    rows: int = 5,
    client: httpx.Client | None = None,
    throttle: CrossrefThrottle | None = None,
) -> list[PaperHit]:
    """Bibliographic title search via ``/works?query.bibliographic=...`` (polite pool).

    Deliberately NOT a :func:`resolve_citation` tier (plan.md §4.1's tier spec is
    S2 -> Crossref DOI canonicalization -> OpenAlex): this is the "bibliographic title
    queries" half of Crossref's plan.md §4.3 role, used by ``cite seed import``
    (Step 7's seed corpus) and available for bibliographic gap-fill.
    """
    payload = _crossref_get(
        CROSSREF_WORKS_URL,
        {"query.bibliographic": query, "rows": str(rows), "mailto": _crossref_mailto()},
        client,
        throttle,
    )
    message = payload.get("message") if isinstance(payload, dict) else None
    items = message.get("items") if isinstance(message, dict) else None
    if not isinstance(items, list):
        raise ResolutionError("Crossref search response shape unexpected: no 'message.items'")
    return [
        hit
        for item in items
        if isinstance(item, dict) and (hit := _parse_crossref_item(item)) is not None
    ]


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------


def _openalex_abstract(inverted: object) -> str | None:
    """Reconstruct plain text from OpenAlex's ``abstract_inverted_index``."""
    if not isinstance(inverted, dict):
        return None
    positions: list[tuple[int, str]] = []
    for word, locations in inverted.items():
        if isinstance(locations, list):
            positions.extend((loc, str(word)) for loc in locations if isinstance(loc, int))
    if not positions:
        return None
    positions.sort()
    return _snippet(" ".join(word for _, word in positions))


def _parse_openalex_item(item: dict[str, Any]) -> PaperHit | None:
    title = item.get("display_name") or item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    doi_raw = item.get("doi")
    year = item.get("publication_year")
    authorships = item.get("authorships")
    authors = tuple(
        str(entry["author"]["display_name"])
        for entry in (authorships if isinstance(authorships, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("author"), dict)
        and isinstance(entry["author"].get("display_name"), str)
    )
    ids = item.get("ids")
    landing = ids.get("openalex") if isinstance(ids, dict) else None
    return PaperHit(
        source="openalex",
        title=title,
        year=year if isinstance(year, int) else None,
        authors=authors,
        doi=normalize_doi(doi_raw) if isinstance(doi_raw, str) else None,
        arxiv_id=None,
        url=landing if isinstance(landing, str) else None,
        abstract_snippet=_openalex_abstract(item.get("abstract_inverted_index")),
        raw=item,
    )


def search_openalex(
    query: str,
    *,
    limit: int = 5,
    client: httpx.Client | None = None,
) -> list[PaperHit]:
    """OpenAlex works search — the fallback tier, gated on the required free key.

    Raises :class:`OpenAlexKeyMissing` BEFORE any request when
    ``CITATION_NEEDED_OPENALEX_KEY`` is unset: reaching this tier without a key is a
    loud, actionable error, never a silent skip.

    The key travels as the ``api_key`` query parameter — OpenAlex offers no
    header-based form (unlike S2's ``x-api-key``), so any error text that might embed
    the request URL is scrubbed: a :class:`ResolutionError` leaving this function
    NEVER contains the key (it propagates into CLI output and result notes).
    """
    api_key = os.environ.get(OPENALEX_KEY_ENV)
    if not api_key:
        raise OpenAlexKeyMissing()
    owns_client = client is None
    active = client if client is not None else _build_client()
    try:
        payload = _get_json(
            active,
            OPENALEX_WORKS_URL,
            {"search": query, "per-page": str(limit), "api_key": api_key},
        )
    except ResolutionError as exc:
        # `from None`: the original chain can embed the full request URL (query string
        # carries the key); a printed traceback must not resurrect the unredacted text.
        message = str(exc).replace(api_key, "<redacted>")
        raise ResolutionError(message) from None
    finally:
        if owns_client:
            active.close()
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ResolutionError("OpenAlex response shape unexpected: 'results' not a list")
    return [
        hit
        for item in results
        if isinstance(item, dict) and (hit := _parse_openalex_item(item)) is not None
    ]


# ---------------------------------------------------------------------------
# Tiered orchestrator
# ---------------------------------------------------------------------------


def resolve_citation(
    query: str,
    *,
    limit: int = 5,
    client: httpx.Client | None = None,
    throttle: CrossrefThrottle | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ResolutionResult:
    """Tiered resolution: S2 -> (DOI found? canonicalize via Crossref) -> OpenAlex.

    Tier semantics:

    - A Semantic Scholar MISS (zero hits) falls through to OpenAlex. An S2 ERROR is
      logged, recorded in ``notes``, and also falls through — but if OpenAlex then
      errors too, that error raises (both tiers down is not a quiet no-hit).
    - :class:`OpenAlexKeyMissing` always raises when the OpenAlex tier is reached
      without a key — never converted into a "no result".
    - A hit carrying a DOI is canonicalized via Crossref; a canonicalization failure
      (e.g. a DataCite/arXiv DOI Crossref does not register) keeps the hit and records
      the failure in ``notes`` — the API echo of the winning tier stands as the record.

    READ-ONLY: this never writes the database; inserts happen only through review flows
    calling ``verify.insert_citation``.
    """
    owns_client = client is None
    active = client if client is not None else _build_client()
    tiers_tried: list[str] = []
    notes: list[str] = []
    try:
        hit: PaperHit | None = None
        tier: str | None = None
        tiers_tried.append("semantic_scholar")
        try:
            hits = search_semantic_scholar(query, limit=limit, client=active, sleep=sleep)
        except ResolutionError as exc:
            _LOGGER.warning("Semantic Scholar tier failed for %r: %s", query, exc)
            notes.append(f"semantic_scholar error: {exc}")
            hits = []
        if hits:
            hit, tier = hits[0], "semantic_scholar"
        else:
            tiers_tried.append("openalex")
            openalex_hits = search_openalex(query, limit=limit, client=active)
            if openalex_hits:
                hit, tier = openalex_hits[0], "openalex"
        crossref_echo: dict[str, Any] | None = None
        # Crossref appears in the tier chain ONLY as DOI canonicalization — plan.md
        # §4.1 specifies S2 -> (Crossref canonicalize) -> OpenAlex. search_crossref
        # (bibliographic title search) is deliberately not a tier here; its consumer
        # is `cite seed import` (Step 7). See its docstring.
        if hit is not None and hit.doi:
            tiers_tried.append("crossref")
            try:
                crossref_echo = lookup_crossref_doi(hit.doi, client=active, throttle=throttle)
            except ResolutionError as exc:
                _LOGGER.warning("Crossref canonicalization failed for %s: %s", hit.doi, exc)
                notes.append(f"crossref canonicalization failed: {exc}")
        return ResolutionResult(
            query=query,
            resolved=hit is not None,
            tier=tier,
            hit=hit,
            crossref_echo=crossref_echo,
            tiers_tried=tuple(tiers_tried),
            notes=tuple(notes),
        )
    finally:
        if owns_client:
            active.close()


__all__ = [
    "CROSSREF_MAILTO_ENV",
    "DEFAULT_CROSSREF_MAILTO",
    "OPENALEX_KEY_ENV",
    "RETRY_AFTER_CAP_S",
    "S2_KEY_ENV",
    "CrossrefThrottle",
    "OpenAlexKeyMissing",
    "PaperHit",
    "ResolutionError",
    "ResolutionResult",
    "lookup_crossref_doi",
    "normalize_doi",
    "resolve_citation",
    "search_crossref",
    "search_openalex",
    "search_semantic_scholar",
]
