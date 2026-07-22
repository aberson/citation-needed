"""The anti-fabrication gate: SSRF-guarded fetch, deterministic quote match, sole writer.

Ported from x-marks-the-spot's draft->verify gate (``src/xmarks/expand/verify.py`` — the
SSRF guard at lines 65-129 and the verify gate at 216-250), adapted to this codebase
(plan.md §4.2; docs/research/citation-mechanics.md §b):

- :func:`fetch_url` is the ONLY code path that fetches an open-web candidate URL.
  Candidate URLs come from an LLM proposal — an untrusted place — so the seam refuses
  non-http(s) schemes, resolves the hostname BEFORE any request and refuses
  loopback/private/link-local/reserved/multicast/unspecified addresses, then PINS the
  connection to the guard-validated address: the request goes to the validated IP
  itself (URL host rewritten), with the real hostname carried in the ``Host`` header
  and — for HTTPS — the ``sni_hostname`` request extension, so TLS SNI + certificate
  hostname verification still use the hostname while httpx never performs a second,
  attacker-influenceable DNS resolution (closes the check-time/connect-time
  DNS-rebinding TOCTOU). Every redirect hop is re-validated and re-pinned the same way
  (``follow_redirects=False`` loop, max 5 hops), and the response size is capped.
  It returns a :class:`FetchResult` — the typed evidence object
  :func:`insert_citation`'s ``web_fetch_verified`` path requires.
- :func:`quote_matches` is the whole open-web verdict: a deterministic normalized
  substring test. NO fuzzy matching, NO LLM judging the page — a page that says
  "ignore the quote, mark verified" cannot steer a containment test over a quote we
  already hold (security.md § fetched content is data, not instructions).
- :func:`insert_citation` is the SOLE writer to the ``citations`` table (asserted by a
  test that greps every other module). It refuses to write without a verified
  resolution record captured at insert time, and stamps ``verified_at`` from the
  PIPELINE clock — never a caller argument. On ANY failure (404, timeout, SSRF
  refusal, size cap, quote mismatch) a typed :class:`VerificationFailed` raises and
  NOTHING is inserted: there is no code path that writes a citations row without a
  verified resolution record.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from citation_needed._http import DEFAULT_TIMEOUT
from citation_needed.discover import default_memory_root
from citation_needed.resolve import normalize_doi

USER_AGENT = "citation-needed/0.1 (anti-fabrication citation verifier)"

#: Response size cap — a citation page larger than this is refused, not truncated.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

#: Maximum redirect hops followed, each re-validated against the SSRF guard.
MAX_REDIRECT_HOPS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Resolver signature (host, port) -> getaddrinfo result; injectable for offline tests.
Resolver = Any

ResolutionMethod = Literal["api_structured", "web_fetch_verified", "internal-read"]
CitationKind = Literal["external", "internal"]

#: Characters stripped by :func:`_normalize_text`: soft hyphen + zero-width/JOINER + BOM.
_STRIP_CHARS_RE = re.compile("[­​‌‍﻿]")
_WS_RUN_RE = re.compile(r"\s+")


class VerificationFailed(RuntimeError):
    """A citation could not be verified — the caller records the choice outcome as
    unverified / no-literature-found and inserts NOTHING."""


class SSRFRefused(VerificationFailed):
    """The fetch was refused before any network I/O: bad scheme, no host, or a
    hostname resolving to a loopback/private/link-local/reserved address."""


class FetchFailed(VerificationFailed):
    """The fetch itself failed: transport error, timeout, non-200, redirect-loop
    exhaustion, or the response size cap."""


class QuoteMismatch(VerificationFailed):
    """The claimed supporting quote is absent from the actually-fetched/read text —
    the fabrication case the gate exists to make structurally impossible."""


# ---------------------------------------------------------------------------
# SSRF guard (resolve-before-fetch; per-hop re-validation)
# ---------------------------------------------------------------------------


def _ip_is_internal(ip: str) -> bool:
    """True if ``ip`` is a non-public address we must never fetch (fail closed).

    Covers loopback, link-local (cloud metadata ``169.254.169.254``), RFC1918 private,
    unspecified, reserved, and multicast — for both address families, unwrapping
    IPv4-mapped IPv6 forms (``::ffff:127.0.0.1``). An unparseable string is internal.
    """
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_unspecified
        or addr.is_reserved
        or addr.is_multicast
    )


def _validated_public_ips(host: str, resolver: Resolver) -> list[str] | None:
    """Resolve ``host`` and return its validated public addresses, refusing internals.

    A literal IP is checked directly (no resolver call) and returns ``None`` — no DNS
    was involved, so there is nothing to pin. A hostname goes through ``resolver``
    (``socket.getaddrinfo`` in production; injectable for offline tests); ONE internal
    answer among the results is enough to refuse — this rejects split-horizon answers
    outright. Resolution happens BEFORE any request, so a blocked host never reaches
    the network; the returned list is what :func:`_pin_validated_target` pins the
    actual connection to, so a later, different DNS answer can never be used.
    """
    if not host:
        raise SSRFRefused("refusing to fetch a URL with no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # not a literal IP — resolve below
    else:
        if _ip_is_internal(host):
            raise SSRFRefused(f"refusing to fetch {host!r}: internal address")
        return None
    try:
        infos = resolver(host, None)
    except (socket.gaierror, OSError) as exc:
        raise FetchFailed(f"could not resolve host {host!r}: {exc}") from exc
    ips: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if _ip_is_internal(ip):
            raise SSRFRefused(f"refusing to fetch {host!r}: resolves to internal address {ip}")
        ips.append(ip)
    if not ips:
        raise FetchFailed(f"could not resolve host {host!r}: resolver returned no addresses")
    return ips


@dataclass(frozen=True)
class _PinnedRequest:
    """One guard-validated hop, ready to issue without any further DNS resolution."""

    url: str  # host rewritten to the validated IP (unchanged for literal-IP URLs)
    host_header: str | None  # original hostname[:port] when pinned, else None
    sni_hostname: str | None  # original hostname for HTTPS SNI/cert check when pinned


def _pin_validated_target(url: str, resolver: Resolver) -> _PinnedRequest:
    """Scheme allowlist + SSRF validation + IP pinning for one URL (initial or hop).

    The request that actually goes out targets the VALIDATED IP (URL host rewritten;
    IPv6 literals bracketed), with the original hostname carried in the ``Host`` header
    and — for HTTPS — the ``sni_hostname`` request extension (httpcore honors it), so
    TLS SNI + certificate hostname verification still use the real hostname. httpx
    therefore never re-resolves the hostname at connect time: check-time and
    connect-time addresses cannot diverge, which closes the DNS-rebinding TOCTOU that
    a resolve-then-fetch-by-name guard leaves open.
    """
    # UNTRUSTED-INPUT PARSE GUARD — same bug-shape as the iter-1 Retry-After crash
    # (an unguarded parse of untrusted input escaping as a bare untyped error):
    # ``urlsplit`` raises ValueError on an unbalanced IPv6 bracket (http://[::1/x),
    # and ``.port`` raises ValueError on a non-numeric / negative / >65535 port.
    # This URL comes from an LLM-proposed candidate or a server-controlled redirect
    # Location — both untrusted — so the WHOLE parse (split + hostname + port) is
    # guarded and malformed input refuses with the module's typed refusal
    # (:class:`SSRFRefused`: refused before any network I/O), never a bare ValueError.
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError as exc:
        raise SSRFRefused(f"refusing to fetch malformed URL {url!r}: {exc}") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise SSRFRefused(f"refusing to fetch non-http(s) URL {url!r}")
    if parts.username is not None or parts.password is not None:
        # Embedded credentials are a classic URL-parser-confusion SSRF shape
        # (http://trusted@evil/) and would be silently dropped by the host rewrite.
        raise SSRFRefused(f"refusing to fetch URL with embedded credentials: {url!r}")
    ips = _validated_public_ips(host, resolver)
    if ips is None:  # literal-IP URL, already validated — nothing to pin
        return _PinnedRequest(url=url, host_header=None, sni_hostname=None)
    pinned_ip = ips[0]
    ip_literal = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    netloc = ip_literal if port is None else f"{ip_literal}:{port}"
    return _PinnedRequest(
        url=urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)),
        host_header=host if port is None else f"{host}:{port}",
        sni_hostname=host if parts.scheme.lower() == "https" else None,
    )


@dataclass(frozen=True)
class FetchResult:
    """The typed evidence object :func:`fetch_url` returns for one guarded fetch.

    :func:`insert_citation`'s ``web_fetch_verified`` path REQUIRES an instance of this
    class — never a bare string — so an "I fetched it" claim is structurally coupled to
    an actual SSRF-guarded fetch: fabricating one now requires deliberately
    constructing a :class:`FetchResult`, and :func:`fetch_url` is the sole legitimate
    producer.
    """

    final_url: str  # hostname-form URL of the hop that returned 200 (post-redirects)
    fetched_text: str  # the raw decoded page text
    fetch_time: str  # pipeline-clock UTC timestamp of the fetch
    hops: int  # redirect hops followed to reach final_url (0 = direct)


def fetch_url(
    url: str,
    client: httpx.Client | None = None,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_hops: int = MAX_REDIRECT_HOPS,
    resolver: Resolver = socket.getaddrinfo,
) -> FetchResult:
    """SSRF-guarded GET returning a :class:`FetchResult` with the raw decoded page text.

    Every hop is validated AND PINNED via :func:`_pin_validated_target`: the request
    goes to the guard-validated IP itself while the ``Host`` header and (for HTTPS) the
    ``sni_hostname`` extension carry the real hostname — httpx never re-resolves the
    name at connect time, so a DNS-rebinding answer served between check and connect
    is never used. Redirects are NEVER auto-followed: each hop's ``Location`` is joined
    against the hostname-form URL, then re-resolved, re-checked, and re-pinned
    (``follow_redirects=False`` is forced per-request even on an injected client), up
    to ``max_hops`` hops. The response body is streamed and refused past ``max_bytes``.
    Any failure raises a typed :class:`VerificationFailed` subclass; nothing is ever
    silently truncated/skipped.
    """
    owns_client = client is None
    active = client if client is not None else httpx.Client(timeout=DEFAULT_TIMEOUT)
    current = url
    try:
        for hop in range(max_hops + 1):
            pinned = _pin_validated_target(current, resolver)
            headers = {"User-Agent": USER_AGENT}
            if pinned.host_header is not None:
                headers["Host"] = pinned.host_header
            extensions: dict[str, Any] | None = (
                {"sni_hostname": pinned.sni_hostname} if pinned.sni_hostname else None
            )
            try:
                with active.stream(
                    "GET",
                    pinned.url,
                    headers=headers,
                    follow_redirects=False,
                    extensions=extensions,
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise FetchFailed(f"redirect without Location from {current!r}")
                        # Join against the hostname-form URL (not the pinned-IP URL) so
                        # relative redirects keep the hostname and re-pin next hop.
                        # Same untrusted-parse bug-shape as _pin_validated_target's
                        # guard (and the iter-1 Retry-After crash): the Location value
                        # is server-controlled, and httpx.InvalidURL is NOT an
                        # httpx.HTTPError — unguarded, a malformed Location would
                        # escape fetch_url untyped.
                        try:
                            current = str(httpx.URL(current).join(location))
                        except (httpx.InvalidURL, ValueError) as exc:
                            raise FetchFailed(
                                f"malformed redirect Location {location!r} from {current!r}: {exc}"
                            ) from exc
                        continue
                    if response.status_code != 200:
                        raise FetchFailed(f"HTTP {response.status_code} fetching {current!r}")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchFailed(
                                f"response exceeds the {max_bytes}-byte cap: {current!r}"
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    encoding = response.charset_encoding or "utf-8"
                    try:
                        text = raw.decode(encoding, errors="replace")
                    except LookupError:
                        text = raw.decode("utf-8", errors="replace")
                    return FetchResult(
                        final_url=current,
                        fetched_text=text,
                        fetch_time=_pipeline_now(),
                        hops=hop,
                    )
            except httpx.HTTPError as exc:
                raise FetchFailed(f"could not fetch {current!r}: {exc}") from exc
        raise FetchFailed(f"redirect chain exceeded {max_hops} hops starting from {url!r}")
    finally:
        if owns_client:
            active.close()


# ---------------------------------------------------------------------------
# Deterministic quote match (the whole open-web verdict — no fuzz, no LLM)
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    """The documented normalization policy — nothing more, nothing fuzzier.

    1. Unicode NFKC (folds ligatures/width/compatibility forms: ``ﬁ`` -> ``fi``).
    2. Strip soft hyphens and zero-width characters (ZWSP/ZWNJ/ZWJ/BOM) — invisible
       codepoints PDFs and CMSes inject mid-word.
    3. Collapse every whitespace run (spaces, tabs, newlines, NBSP via NFKC) to one
       space; strip ends.
    4. Case-fold.

    Deterministic and side-effect-free; the verdict is plain substring containment.
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _STRIP_CHARS_RE.sub("", normalized)
    return _WS_RUN_RE.sub(" ", normalized).strip().casefold()


def quote_matches(fetched_text: str, quote: str) -> bool:
    """True iff the normalized ``quote`` appears verbatim in the normalized text.

    An empty/whitespace-only quote never matches — there is nothing to verify.
    """
    needle = _normalize_text(quote)
    return bool(needle) and needle in _normalize_text(fetched_text)


# ---------------------------------------------------------------------------
# insert_citation — THE sole writer to the citations table
# ---------------------------------------------------------------------------


def _pipeline_now() -> str:
    """The pipeline clock — ``verified_at`` comes from here, never from a caller."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_url(url: str) -> str:
    """Normalized URL form for the natural key: lowercase scheme+host, no fragment,
    no trailing slash on the path. Query strings are preserved (they select content).

    A malformed URL (e.g. unbalanced IPv6 bracket) refuses typed
    (:class:`VerificationFailed`), never a bare ValueError — same untrusted-parse
    guard discipline as :func:`_pin_validated_target`."""
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise VerificationFailed(f"cannot normalize malformed URL {url!r}: {exc}") from exc
    path = parts.path.rstrip("/")
    normalized = f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"
    if parts.query:
        normalized += f"?{parts.query}"
    return normalized


def _natural_key(kind: CitationKind, doi: str | None, url: str | None, path: str | None) -> str:
    """Dedup identity: normalized DOI, else normalized URL, else workspace path."""
    if doi:
        return normalize_doi(doi)
    if url:
        return normalize_url(url)
    if path:
        return path
    raise VerificationFailed(f"no locator to derive a natural key for kind={kind!r}")


MEMORY_PATH_SCHEME = "memory:"


def _confined_internal_path(
    normalized_path: str, workspace_root: Path, memory_root: Path | None
) -> Path:
    """Resolve an internal-read locator to a file CONFINED to its root — or refuse.

    ``workspace_path`` comes from the untrusted commit payload, so the join is a
    traversal sink: ``../`` climbs out of the root and an absolute path RESETS a
    ``pathlib`` join entirely (``Path(root) / 'C:/x'`` discards ``root``). Every
    candidate is therefore ``resolve()``d and REQUIRED to be ``is_relative_to`` its
    root; any escape raises :class:`VerificationFailed` (whole-payload reject —
    commit's one transaction writes nothing).

    Two locator families:

    - workspace-relative (``docs/x.md``) — confined to ``workspace_root``;
    - ``memory:<project-dir-slug>/<file>.md`` (discover.py's scheme for memory
      artifacts, which live OUTSIDE the workspace under
      ``<memory_root>/<slug>/memory/``) — confined to ``memory_root``
      (:func:`citation_needed.discover.default_memory_root` when not supplied).
    """
    if normalized_path.startswith(MEMORY_PATH_SCHEME):
        root = Path(memory_root) if memory_root is not None else default_memory_root()
        remainder = normalized_path[len(MEMORY_PATH_SCHEME) :]
        dir_slug, sep, rel_file = remainder.partition("/")
        if not dir_slug or not sep or not rel_file:
            raise VerificationFailed(
                f"internal-read refuses {normalized_path!r}: a memory locator must be "
                "memory:<project-dir-slug>/<file>.md"
            )
        candidate = root / dir_slug / "memory" / rel_file
        label = "memory root"
    else:
        root = Path(workspace_root)
        candidate = root / normalized_path
        label = "workspace root"
    try:
        resolved = candidate.resolve()
        resolved_root = root.resolve()
    except OSError as exc:  # e.g. an un-resolvable path on this OS
        raise VerificationFailed(
            f"internal-read refuses {normalized_path!r}: cannot resolve ({exc})"
        ) from exc
    if not resolved.is_relative_to(resolved_root):
        raise VerificationFailed(
            f"internal-read refuses {normalized_path!r}: the path escapes the "
            f"{label} {resolved_root.as_posix()} — internal citations are workspace/"
            "memory provenance only, never arbitrary machine paths"
        )
    return resolved


def insert_citation(
    conn: sqlite3.Connection,
    *,
    kind: CitationKind,
    resolution_method: ResolutionMethod,
    title: str,
    doi: str | None = None,
    url: str | None = None,
    workspace_path: str | None = None,
    workspace_root: Path | None = None,
    memory_root: Path | None = None,
    supporting_quote: str | None = None,
    fetch_result: FetchResult | None = None,
    api_echo: dict[str, Any] | None = None,
    authors: str | None = None,
    year: int | None = None,
    venue: str | None = None,
    keywords: str | None = None,
    notes: str | None = None,
    source_git_sha: str | None = None,
    source_line_ref: str | None = None,
) -> int:
    """THE sole writer to ``citations``. Verifies the resolution record, then inserts.

    The NOT NULL resolution record, enforced here per method (schema CHECKs are the
    structural backstop — ``kind='external'`` requires ``url_or_doi``; the
    ``resolution_method`` enum has no ``llm_claimed`` state to occupy):

    - ``api_structured`` (external): requires ``api_echo`` — the API's own JSON
      response, captured by resolve.py from the actual HTTP call. Stored verbatim
      (compact JSON) as the supporting quote.
    - ``web_fetch_verified`` (external): requires ``supporting_quote`` AND
      ``fetch_result`` — the :class:`FetchResult` INSTANCE :func:`fetch_url` returned
      at verify time (type-enforced; a bare string is refused, so a caller cannot pass
      hallucinated page text without deliberately constructing the evidence type); the
      quote must pass :func:`quote_matches` against its raw ``fetched_text`` or
      :class:`QuoteMismatch` raises and nothing is inserted.
    - ``internal-read`` (internal): requires ``workspace_path`` + ``workspace_root`` +
      ``supporting_quote``; this function READS the actual file and the quoted span
      must appear in it — a workspace claim is verified against the file, not trusted.
      The path is CONFINED (:func:`_confined_internal_path`): resolved and required to
      stay inside ``workspace_root`` (or, for ``memory:``-scheme locators, inside
      ``memory_root``); a ``../`` or absolute-path escape refuses, nothing inserted.

    ``verified_at`` is stamped from the pipeline clock (:func:`_pipeline_now`) at
    insert time — it is deliberately NOT a parameter.

    Dedup: ``UNIQUE(kind, natural_key)`` where the natural key is the normalized DOI,
    else the normalized URL, else the workspace path. Re-inserting an existing citation
    is idempotent: the existing row's id returns (its ``verified_at`` refreshed, since
    this call did re-verify) and no duplicate row is created. The dedup is an atomic
    upsert (``ON CONFLICT DO NOTHING`` + same-transaction re-select), never a
    check-then-insert, so concurrent connections inserting the same identity cannot
    raise ``IntegrityError``.

    Transactions belong to the caller (`with conn:`); this function only executes.
    """
    if not title or not title.strip():
        raise VerificationFailed("resolution record incomplete: retrieved title is required")

    url_or_doi: str | None = None
    normalized_path: str | None = None
    if kind == "external":
        if not doi and not url:
            raise VerificationFailed(
                "kind='external' requires a locator (DOI or URL) captured from the "
                "actual fetch/API response"
            )
        url_or_doi = f"https://doi.org/{normalize_doi(doi)}" if doi else url
    elif kind == "internal":
        if not workspace_path:
            raise VerificationFailed("kind='internal' requires workspace_path")
        normalized_path = workspace_path.replace("\\", "/")
    else:  # pragma: no cover - Literal-typed, but fail loud on an untyped caller
        raise VerificationFailed(f"unknown citation kind {kind!r}")

    quote_to_store: str | None
    if resolution_method == "api_structured":
        if kind != "external":
            raise VerificationFailed("api_structured citations must be kind='external'")
        if not api_echo:
            raise VerificationFailed(
                "api_structured requires api_echo — the API's own JSON response "
                "captured from the actual call"
            )
        quote_to_store = json.dumps(api_echo, ensure_ascii=False, sort_keys=True)
    elif resolution_method == "web_fetch_verified":
        if kind != "external":
            raise VerificationFailed("web_fetch_verified citations must be kind='external'")
        if not supporting_quote:
            raise VerificationFailed("web_fetch_verified requires a supporting quote")
        if not isinstance(fetch_result, FetchResult):
            raise VerificationFailed(
                "web_fetch_verified requires the FetchResult instance fetch_url returned "
                "at verify time — a bare string (or None) is not fetch evidence"
            )
        if not quote_matches(fetch_result.fetched_text, supporting_quote):
            raise QuoteMismatch(
                "supporting quote not found in the fetched text — refusing to insert "
                "an unverified (fabricated?) citation"
            )
        quote_to_store = supporting_quote
    elif resolution_method == "internal-read":
        if kind != "internal":
            raise VerificationFailed("internal-read citations must be kind='internal'")
        if not supporting_quote:
            raise VerificationFailed("internal-read requires the quoted span")
        if workspace_root is None:
            raise VerificationFailed(
                "internal-read requires workspace_root so the span can be read from the actual file"
            )
        assert normalized_path is not None  # guaranteed by the kind='internal' branch
        file_path = _confined_internal_path(normalized_path, workspace_root, memory_root)
        try:
            file_text = file_path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise VerificationFailed(
                f"internal-read could not read {file_path.as_posix()}: {exc}"
            ) from exc
        if not quote_matches(file_text, supporting_quote):
            raise QuoteMismatch(f"quoted span not found in {normalized_path} — refusing to insert")
        quote_to_store = supporting_quote
    else:  # pragma: no cover - Literal-typed, but fail loud on an untyped caller
        raise VerificationFailed(f"unknown resolution_method {resolution_method!r}")

    natural_key = _natural_key(kind, doi, url, normalized_path)
    verified_at = _pipeline_now()

    # Atomic dedup: INSERT ... ON CONFLICT DO NOTHING, then (in the SAME implicit
    # transaction) select the surviving row. A SELECT-then-INSERT pair is a race under
    # concurrent connections (db.connect's WAL + busy_timeout exist exactly because a
    # sweep fans out overlapping verify/insert calls): both SELECTs see no row, the
    # second INSERT raises IntegrityError. The upsert form cannot.
    cursor = conn.execute(
        "INSERT INTO citations (kind, natural_key, title, authors, year, venue, url_or_doi, "
        "workspace_path, verified_at, resolution_method, supporting_quote, keywords, "
        "source_git_sha, source_line_ref, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, natural_key) DO NOTHING",
        (
            kind,
            natural_key,
            title.strip(),
            authors,
            year,
            venue,
            url_or_doi,
            normalized_path,
            verified_at,
            resolution_method,
            quote_to_store,
            keywords,
            source_git_sha,
            source_line_ref,
            notes,
        ),
    )
    if cursor.rowcount == 1:
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    # Conflict path — idempotent reuse: same identity -> same row. verified_at
    # refreshes because THIS call re-verified; nothing else on the row is touched.
    row = conn.execute(
        "SELECT id FROM citations WHERE kind = ? AND natural_key = ?",
        (kind, natural_key),
    ).fetchone()
    if row is None:  # pragma: no cover — conflicting row vanished mid-transaction
        raise VerificationFailed(
            f"citation upsert conflicted but no row exists for ({kind!r}, {natural_key!r})"
        )
    conn.execute("UPDATE citations SET verified_at = ? WHERE id = ?", (verified_at, row[0]))
    return int(row[0])


__all__ = [
    "MAX_REDIRECT_HOPS",
    "MAX_RESPONSE_BYTES",
    "MEMORY_PATH_SCHEME",
    "FetchFailed",
    "FetchResult",
    "QuoteMismatch",
    "SSRFRefused",
    "VerificationFailed",
    "fetch_url",
    "insert_citation",
    "normalize_url",
    "quote_matches",
]
