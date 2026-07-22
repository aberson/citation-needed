"""verify.py — SSRF guard fixtures, deterministic quote match, insert_citation gate.

The anti-fabrication acceptance lives here: a fabricated citation (quote absent from the
fetched text) raises typed and inserts NOTHING; every SSRF fixture (loopback, private,
link-local, redirect-hop-to-private) refuses before any request; the connection is
PINNED to the guard-validated IP (Host header + SNI carry the hostname) so check-time
and connect-time DNS cannot diverge; ``verified_at`` comes from the pipeline clock,
never a caller argument; ``insert_citation`` is asserted to be the sole writer to the
citations table and is concurrency-safe (atomic upsert, two-thread regression).
"""

from __future__ import annotations

import inspect
import re
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from citation_needed import db, verify

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "citation_needed"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "cite.db"
    db.init_db(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def _citation_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0])


def _recording_client(handler_calls: list[str]) -> httpx.Client:
    """A client whose transport records every request — SSRF tests assert it stays empty."""

    def handler(request: httpx.Request) -> httpx.Response:
        handler_calls.append(str(request.url))
        return httpx.Response(200, text="should never be reached")

    return httpx.Client(transport=httpx.MockTransport(handler))


#: Offline resolver: known fake-public hosts resolve; anything else is a test bug.
_PUBLIC_HOSTS = {
    "pub.test": "93.184.216.34",
    "pub2.test": "151.101.1.140",
    "pub6.test": "2606:2800:220:1:248:1893:25c8:1946",  # public IPv6 (bracket pinning)
}


def _fake_resolver(host: str, port: object) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    if host in _PUBLIC_HOSTS:
        return [(2, 1, 6, "", (_PUBLIC_HOSTS[host], 0))]
    if host == "internal.test":
        return [(2, 1, 6, "", ("10.0.0.5", 0))]
    if host == "rebind.test":
        # Split answer: one public, one internal — ONE internal answer must refuse.
        return [(2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("192.168.1.9", 0))]
    raise AssertionError(f"unexpected resolver call for {host!r}")


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",  # loopback
        "http://127.8.9.10/",  # loopback (whole /8)
        "http://[::1]/secret",  # IPv6 loopback
        "http://10.1.2.3/x",  # RFC1918 private
        "http://192.168.1.5/x",  # RFC1918 private
        "http://172.16.0.9/x",  # RFC1918 private (172.16/12)
        "http://169.254.169.254/latest/meta-data/",  # link-local / cloud metadata
        "http://0.0.0.0/",  # unspecified
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
    ],
)
def test_fetch_refuses_internal_literal_addresses(url: str) -> None:
    calls: list[str] = []
    with _recording_client(calls) as client, pytest.raises(verify.SSRFRefused):
        verify.fetch_url(url, client)
    assert calls == []  # refused BEFORE any request


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://x/"])
def test_fetch_refuses_non_http_schemes(url: str) -> None:
    calls: list[str] = []
    with _recording_client(calls) as client, pytest.raises(verify.SSRFRefused, match="non-http"):
        verify.fetch_url(url, client)
    assert calls == []


def test_fetch_refuses_url_without_host() -> None:
    with pytest.raises(verify.SSRFRefused, match="no host"):
        verify.fetch_url("http:///path-only")


def test_fetch_refuses_hostname_resolving_to_private() -> None:
    calls: list[str] = []
    with (
        _recording_client(calls) as client,
        pytest.raises(verify.SSRFRefused, match="internal address"),
    ):
        verify.fetch_url("http://internal.test/x", client, resolver=_fake_resolver)
    assert calls == []


def test_fetch_refuses_split_dns_answer() -> None:
    """One internal answer among the resolved set refuses (split-horizon defense)."""
    calls: list[str] = []
    with _recording_client(calls) as client, pytest.raises(verify.SSRFRefused):
        verify.fetch_url("http://rebind.test/x", client, resolver=_fake_resolver)
    assert calls == []


def test_fetch_pins_connection_to_validated_ip_with_host_and_sni() -> None:
    """The TOCTOU rebinding closure: the outgoing request targets the guard-validated
    IP itself, while the Host header AND the sni_hostname extension carry the real
    hostname (TLS SNI + certificate verification keep working) — httpx never gets a
    chance to re-resolve the name at connect time."""
    seen: list[tuple[str, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url),
                request.headers.get("Host"),
                request.extensions.get("sni_hostname"),
            )
        )
        return httpx.Response(200, text="pinned body")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify.fetch_url("https://pub.test/paper", client, resolver=_fake_resolver)
    assert result.fetched_text == "pinned body"
    assert result.final_url == "https://pub.test/paper"
    assert result.hops == 0
    assert seen == [("https://93.184.216.34/paper", "pub.test", "pub.test")]


def test_fetch_pinning_http_omits_sni_and_preserves_port() -> None:
    """Plain http: no SNI extension; an explicit port survives in both URL and Host."""
    seen: list[tuple[str, str | None, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url),
                request.headers.get("Host"),
                request.extensions.get("sni_hostname"),
            )
        )
        return httpx.Response(200, text="ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verify.fetch_url("http://pub.test:8080/x?q=1", client, resolver=_fake_resolver)
    assert seen == [("http://93.184.216.34:8080/x?q=1", "pub.test:8080", None)]


def test_fetch_pins_ipv6_answer_with_brackets() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("Host")))
        return httpx.Response(200, text="v6 body")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify.fetch_url("http://pub6.test/x", client, resolver=_fake_resolver)
    assert result.fetched_text == "v6 body"
    assert seen == [("http://[2606:2800:220:1:248:1893:25c8:1946]/x", "pub6.test")]


def test_fetch_literal_public_ip_passes_through_unpinned() -> None:
    """A literal public IP involved no DNS — nothing to pin, no Host override."""
    seen: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.extensions.get("sni_hostname")))
        return httpx.Response(200, text="direct")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify.fetch_url("http://93.184.216.34/x", client, resolver=_fake_resolver)
    assert result.fetched_text == "direct"
    assert seen == [("http://93.184.216.34/x", None)]


def test_fetch_refuses_url_with_embedded_credentials() -> None:
    """user:pass@host is a URL-parser-confusion SSRF shape; refuse before any request."""
    calls: list[str] = []
    with (
        _recording_client(calls) as client,
        pytest.raises(verify.SSRFRefused, match="credentials"),
    ):
        verify.fetch_url("http://trusted.example@pub.test/x", client, resolver=_fake_resolver)
    assert calls == []


def test_fetch_redirect_hop_to_private_is_refused_and_never_fetched() -> None:
    """Hop 2 pointing at a private address refuses; only hop 1 ever hit the transport."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.headers.get("Host") == "pub.test":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/steal"})
        raise AssertionError(f"the private hop must never be fetched: {request.url}")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(verify.SSRFRefused),
    ):
        verify.fetch_url("http://pub.test/start", client, resolver=_fake_resolver)
    assert calls == ["http://93.184.216.34/start"]  # hop 1 went to the PINNED IP


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:99999/x",  # port out of range (.port raises ValueError)
        "http://example.com:-1/x",  # negative port
        "http://example.com:abc/x",  # non-numeric port
        "http://[::1/x",  # unbalanced IPv6 bracket (urlsplit itself raises)
    ],
)
def test_fetch_refuses_malformed_url_typed_before_any_request(url: str) -> None:
    """Malformed candidate URLs refuse TYPED (SSRFRefused), never a bare ValueError.

    Same bug-shape as the iter-1 Retry-After crash: an unguarded parse of untrusted
    input. urlsplit / .port raise bare ValueError on these shapes; the guard converts
    them to the module's typed refusal BEFORE any request reaches the transport.
    """
    calls: list[str] = []
    with (
        _recording_client(calls) as client,
        pytest.raises(verify.SSRFRefused, match="malformed URL"),
    ):
        verify.fetch_url(url, client, resolver=_fake_resolver)
    assert calls == []  # refused BEFORE any request


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        # httpx.URL.join itself rejects a non-numeric port -> caught at the join guard.
        ("http://example.com:abc/x", verify.FetchFailed),
        # join accepts an out-of-range port -> caught at the next hop's re-pin guard.
        ("http://example.com:99999/x", verify.SSRFRefused),
    ],
)
def test_fetch_redirect_with_malformed_location_refused_typed(
    location: str, expected: type[verify.VerificationFailed]
) -> None:
    """A malformed server-controlled Location refuses typed; only hop 1 was fetched."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": location})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(expected),
    ):
        verify.fetch_url("http://pub.test/start", client, resolver=_fake_resolver)
    assert calls == ["http://93.184.216.34/start"]  # the malformed hop never fetched


def test_fetch_redirect_to_non_http_scheme_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "file:///etc/passwd"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(verify.SSRFRefused, match="non-http"),
    ):
        verify.fetch_url("http://pub.test/start", client, resolver=_fake_resolver)


def test_fetch_follows_safe_redirect_with_per_hop_revalidation() -> None:
    calls: list[str] = []
    hosts_seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        hosts_seen.append(request.headers.get("Host"))
        if request.headers.get("Host") == "pub.test":
            return httpx.Response(307, headers={"Location": "http://pub2.test/final"})
        return httpx.Response(200, text="the real page body")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify.fetch_url("http://pub.test/start", client, resolver=_fake_resolver)
    assert result.fetched_text == "the real page body"
    assert result.final_url == "http://pub2.test/final"  # hostname form, not the pinned IP
    assert result.hops == 1
    # Each hop was re-validated AND re-pinned: the wire saw the validated IPs...
    assert calls == ["http://93.184.216.34/start", "http://151.101.1.140/final"]
    # ...while the Host header carried the real hostname per hop.
    assert hosts_seen == ["pub.test", "pub2.test"]


def test_fetch_redirect_chain_is_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://pub.test/again"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(verify.FetchFailed, match="exceeded"),
    ):
        verify.fetch_url("http://pub.test/loop", client, resolver=_fake_resolver)


def test_fetch_404_raises_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(verify.FetchFailed, match="HTTP 404"),
    ):
        verify.fetch_url("http://pub.test/gone", client, resolver=_fake_resolver)


def test_fetch_size_cap_refuses_not_truncates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 500)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(verify.FetchFailed, match="byte cap"),
    ):
        verify.fetch_url("http://pub.test/big", client, resolver=_fake_resolver, max_bytes=100)


def test_fetch_transport_error_raises_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(verify.FetchFailed, match="could not fetch"),
    ):
        verify.fetch_url("http://pub.test/slow", client, resolver=_fake_resolver)


# ---------------------------------------------------------------------------
# Deterministic quote match (the documented normalization policy, nothing fuzzier)
# ---------------------------------------------------------------------------


def test_quote_matches_whitespace_runs_and_case() -> None:
    page = "Models  perform\n\tBEST when relevant information occurs at the beginning."
    assert verify.quote_matches(page, "models perform best when relevant information")


def test_quote_matches_nfkc_folds_ligatures() -> None:
    assert verify.quote_matches("scientiﬁc veriﬁcation", "scientific verification")


def test_quote_matches_strips_soft_hyphens_and_zero_width() -> None:
    page = "cita­tion veri​fi‍cation pipe‌line﻿ done"
    assert verify.quote_matches(page, "citation verification pipeline done")


def test_quote_matches_is_substring_only_no_fuzz() -> None:
    page = "The quick brown fox jumps over the lazy dog."
    assert not verify.quote_matches(page, "quick red fox")  # one word off = no match
    assert not verify.quote_matches(page, "")  # nothing to verify never matches
    assert not verify.quote_matches("", "quote")


# ---------------------------------------------------------------------------
# insert_citation — the sole writer / anti-fabrication gate
# ---------------------------------------------------------------------------

FETCHED = (
    "We analyze how language models use long contexts. Performance is often highest "
    "when relevant information occurs at the beginning or end of the input context."
)


def _fetch_result(text: str, url: str = "https://example.com/paper") -> verify.FetchResult:
    """Test-side FetchResult construction — the deliberate act the type now requires."""
    return verify.FetchResult(
        final_url=url, fetched_text=text, fetch_time="2026-07-21T12:00:00Z", hops=0
    )


def test_fabricated_quote_refused_and_nothing_inserted(conn: sqlite3.Connection) -> None:
    with pytest.raises(verify.QuoteMismatch):
        verify.insert_citation(
            conn,
            kind="external",
            resolution_method="web_fetch_verified",
            title="Lost in the Middle",
            url="https://example.com/paper",
            supporting_quote="models always prefer information in the exact middle",
            fetch_result=_fetch_result(FETCHED),
        )
    assert _citation_count(conn) == 0  # a fabricated citation is structurally impossible


def test_web_fetch_verified_requires_fetch_result_instance(conn: sqlite3.Connection) -> None:
    """A bare string (hallucinated page text) is not fetch evidence — type-enforced."""
    for bad_evidence in (None, FETCHED):  # missing entirely, and the pre-fix bare-str shape
        with pytest.raises(verify.VerificationFailed, match="FetchResult"):
            verify.insert_citation(
                conn,
                kind="external",
                resolution_method="web_fetch_verified",
                title="Lost in the Middle",
                url="https://example.com/paper",
                supporting_quote="performance is often highest",
                fetch_result=bad_evidence,  # type: ignore[arg-type]
            )
    assert _citation_count(conn) == 0


def test_fetch_url_result_feeds_insert_citation_round_trip(conn: sqlite3.Connection) -> None:
    """Production round trip: fetch_url's FetchResult is exactly what the gate accepts."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FETCHED)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify.fetch_url("http://pub.test/paper", client, resolver=_fake_resolver)
    citation_id = verify.insert_citation(
        conn,
        kind="external",
        resolution_method="web_fetch_verified",
        title="Lost in the Middle",
        url=result.final_url,
        supporting_quote="performance is often highest when relevant information occurs",
        fetch_result=result,
    )
    assert citation_id >= 1
    assert _citation_count(conn) == 1


def test_web_fetch_verified_happy_path(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "_pipeline_now", lambda: "2026-07-21T12:00:00Z")
    citation_id = verify.insert_citation(
        conn,
        kind="external",
        resolution_method="web_fetch_verified",
        title="Lost in the Middle",
        url="https://Example.com/Paper/",
        supporting_quote="performance is often highest when relevant information occurs",
        fetch_result=_fetch_result(FETCHED),
    )
    row = conn.execute(
        "SELECT kind, natural_key, title, url_or_doi, verified_at, resolution_method, "
        "supporting_quote FROM citations WHERE id = ?",
        (citation_id,),
    ).fetchone()
    assert row == (
        "external",
        "https://example.com/Paper",  # normalized: host lowercased, trailing slash dropped
        "Lost in the Middle",
        "https://Example.com/Paper/",
        "2026-07-21T12:00:00Z",  # the pipeline clock, monkeypatched
        "web_fetch_verified",
        "performance is often highest when relevant information occurs",
    )


def test_verified_at_is_never_a_caller_argument() -> None:
    parameters = set(inspect.signature(verify.insert_citation).parameters)
    assert "verified_at" not in parameters
    assert "access_date" not in parameters  # the pipeline clock is not injectable per-call


def test_api_structured_stores_the_json_echo(conn: sqlite3.Connection) -> None:
    echo: dict[str, Any] = {"paperId": "abc123", "title": "Lost in the Middle", "year": 2023}
    citation_id = verify.insert_citation(
        conn,
        kind="external",
        resolution_method="api_structured",
        title="Lost in the Middle",
        doi="https://doi.org/10.48550/arXiv.2307.03172",
        api_echo=echo,
        keywords="long-context retrieval",
    )
    row = conn.execute(
        "SELECT natural_key, url_or_doi, supporting_quote FROM citations WHERE id = ?",
        (citation_id,),
    ).fetchone()
    assert row[0] == "10.48550/arxiv.2307.03172"
    assert row[1] == "https://doi.org/10.48550/arxiv.2307.03172"
    assert '"paperId": "abc123"' in row[2]  # the API's own JSON echo is the record


def test_api_structured_requires_the_echo(conn: sqlite3.Connection) -> None:
    with pytest.raises(verify.VerificationFailed, match="api_echo"):
        verify.insert_citation(
            conn,
            kind="external",
            resolution_method="api_structured",
            title="No Echo",
            doi="10.1/x",
        )
    assert _citation_count(conn) == 0


def test_internal_read_verifies_against_the_actual_file(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "lesson.md").write_text(
        "# Lesson\n\nGrep every consumer of the old shape before landing.\n", encoding="utf-8"
    )
    citation_id = verify.insert_citation(
        conn,
        kind="internal",
        resolution_method="internal-read",
        title="Lessons learned: grep downstream consumers",
        workspace_path="docs\\lesson.md",  # backslashes normalize to forward slashes
        workspace_root=workspace,
        supporting_quote="grep every consumer of the old shape",
    )
    row = conn.execute(
        "SELECT natural_key, workspace_path, supporting_quote FROM citations WHERE id = ?",
        (citation_id,),
    ).fetchone()
    assert row[0] == "docs/lesson.md"
    assert row[1] == "docs/lesson.md"


def test_internal_read_quote_absent_from_file_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.md").write_text("Nothing relevant here.\n", encoding="utf-8")
    with pytest.raises(verify.QuoteMismatch):
        verify.insert_citation(
            conn,
            kind="internal",
            resolution_method="internal-read",
            title="Fabricated internal claim",
            workspace_path="note.md",
            workspace_root=workspace,
            supporting_quote="a span that does not exist in the file",
        )
    assert _citation_count(conn) == 0


def test_internal_read_missing_file_refused(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(verify.VerificationFailed, match="could not read"):
        verify.insert_citation(
            conn,
            kind="internal",
            resolution_method="internal-read",
            title="Ghost file",
            workspace_path="missing.md",
            workspace_root=tmp_path,
            supporting_quote="anything",
        )
    assert _citation_count(conn) == 0


def test_external_requires_a_locator(conn: sqlite3.Connection) -> None:
    with pytest.raises(verify.VerificationFailed, match="locator"):
        verify.insert_citation(
            conn,
            kind="external",
            resolution_method="api_structured",
            title="No locator",
            api_echo={"x": 1},
        )
    assert _citation_count(conn) == 0


def test_title_is_required(conn: sqlite3.Connection) -> None:
    with pytest.raises(verify.VerificationFailed, match="title"):
        verify.insert_citation(
            conn,
            kind="external",
            resolution_method="api_structured",
            title="   ",
            doi="10.1/x",
            api_echo={"x": 1},
        )
    assert _citation_count(conn) == 0


def test_method_kind_pairings_enforced(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(verify.VerificationFailed, match="kind='external'"):
        verify.insert_citation(
            conn,
            kind="internal",
            resolution_method="api_structured",
            title="Wrong pairing",
            workspace_path="x.md",
            api_echo={"x": 1},
        )
    with pytest.raises(verify.VerificationFailed, match="kind='internal'"):
        verify.insert_citation(
            conn,
            kind="external",
            resolution_method="internal-read",
            title="Wrong pairing",
            url="https://example.com/",
            workspace_root=tmp_path,
            supporting_quote="q",
        )
    assert _citation_count(conn) == 0


def test_dedup_on_doi_natural_key_is_idempotent(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "_pipeline_now", lambda: "2026-07-21T10:00:00Z")
    first = verify.insert_citation(
        conn,
        kind="external",
        resolution_method="api_structured",
        title="Lost in the Middle",
        doi="10.48550/arXiv.2307.03172",
        api_echo={"paperId": "abc123"},
    )
    monkeypatch.setattr(verify, "_pipeline_now", lambda: "2026-07-21T11:30:00Z")
    second = verify.insert_citation(
        conn,
        kind="external",
        resolution_method="api_structured",
        title="Lost in the Middle (retitled by a later review)",
        doi="https://doi.org/10.48550/ARXIV.2307.03172",  # same DOI, different shape
        api_echo={"paperId": "abc123", "again": True},
    )
    assert first == second  # same natural key -> same row id, no duplicate
    assert _citation_count(conn) == 1
    verified_at = conn.execute(
        "SELECT verified_at FROM citations WHERE id = ?", (first,)
    ).fetchone()[0]
    assert verified_at == "2026-07-21T11:30:00Z"  # reuse refreshed the re-verification stamp


def test_dedup_on_normalized_url(conn: sqlite3.Connection) -> None:
    quote = "performance is often highest"
    first = verify.insert_citation(
        conn,
        kind="external",
        resolution_method="web_fetch_verified",
        title="Same page",
        url="https://Example.com/paper/",
        supporting_quote=quote,
        fetch_result=_fetch_result(FETCHED),
    )
    second = verify.insert_citation(
        conn,
        kind="external",
        resolution_method="web_fetch_verified",
        title="Same page",
        url="https://example.com/paper",
        supporting_quote=quote,
        fetch_result=_fetch_result(FETCHED),
    )
    assert first == second
    assert _citation_count(conn) == 1


def test_concurrent_inserts_same_identity_are_idempotent(tmp_path: Path) -> None:
    """The reviewer's two-thread repro shape: two connections, one DOI, same instant.

    The pre-fix SELECT-then-INSERT raised sqlite3.IntegrityError from the losing
    thread; the atomic upsert must give BOTH threads the same row id, no exception,
    one row. Real threads, the real production function, WAL mode (db.connect).
    """
    path = tmp_path / "cite.db"
    db.init_db(path)
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[Exception] = []

    def worker() -> None:
        connection = db.connect(path)
        try:
            barrier.wait(timeout=10)
            with connection:
                results.append(
                    verify.insert_citation(
                        connection,
                        kind="external",
                        resolution_method="api_structured",
                        title="Lost in the Middle",
                        doi="10.48550/arXiv.2307.03172",
                        api_echo={"paperId": "abc123"},
                    )
                )
        except Exception as exc:  # the assertion below IS "no exception escaped"
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == [], f"concurrent insert_citation raised: {errors!r}"
    assert len(results) == 2 and results[0] == results[1]  # same identity -> same row
    connection = db.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM citations").fetchone()[0] == 1
    finally:
        connection.close()


def test_normalize_url_malformed_raises_typed(conn: sqlite3.Connection) -> None:
    """The natural-key parse is the third urlsplit of untrusted input in this module —
    a malformed locator refuses typed (and inserts NOTHING), never a bare ValueError."""
    with pytest.raises(verify.VerificationFailed, match="malformed URL"):
        verify.normalize_url("http://[::1/x")
    with pytest.raises(verify.VerificationFailed, match="malformed URL"):
        verify.insert_citation(
            conn,
            kind="external",
            resolution_method="web_fetch_verified",
            title="Malformed locator",
            url="http://[::1/x",
            supporting_quote="performance is often highest",
            fetch_result=_fetch_result(FETCHED),
        )
    assert _citation_count(conn) == 0


def test_insert_citation_is_the_sole_writer() -> None:
    """No module other than verify.py may INSERT/UPDATE/DELETE the citations table.

    Convention enforced by grep (plan.md §4.2): the schema CHECKs are the structural
    backstop, this test keeps application code honest.
    """
    write_pattern = re.compile(
        r"(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+citations\b", re.IGNORECASE
    )
    offenders: list[str] = []
    # rglob, not glob: a future subpackage must not silently escape this guard.
    for module in sorted(SRC_DIR.rglob("*.py")):
        relative = module.relative_to(SRC_DIR).as_posix()
        if relative == "verify.py":
            continue
        if write_pattern.search(module.read_text(encoding="utf-8")):
            offenders.append(relative)
    assert offenders == [], f"citations table written outside verify.py: {offenders}"
