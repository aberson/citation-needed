"""resolve.py — injectable-transport unit tests + CLI integration + one live smoke.

Every network interaction goes through ``httpx.MockTransport`` handlers (the clients'
injectable seam); nothing here touches the real network except the single test marked
``live``, which the default run excludes via addopts (`-m 'not live'`) and the
orchestrator runs separately once (``uv run pytest -m live``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from citation_needed import _http, resolve, verify
from citation_needed.cli import main

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic env for every non-live test (the operator may have real keys set)."""
    if "live" in request.keywords:
        return
    monkeypatch.delenv(resolve.S2_KEY_ENV, raising=False)
    monkeypatch.delenv(resolve.OPENALEX_KEY_ENV, raising=False)
    monkeypatch.delenv(resolve.CROSSREF_MAILTO_ENV, raising=False)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _noop_throttle() -> resolve.CrossrefThrottle:
    clock = _FakeClock()
    return resolve.CrossrefThrottle(clock=clock.monotonic, sleep=clock.sleep)


S2_PAYLOAD = {
    "total": 1,
    "offset": 0,
    "data": [
        {
            "paperId": "abc123",
            "title": "Lost in the Middle: How Language Models Use Long Contexts",
            "year": 2023,
            "authors": [{"authorId": "1", "name": "Nelson F. Liu"}, {"name": "Percy Liang"}],
            "externalIds": {"DOI": "10.48550/arXiv.2307.03172", "ArXiv": "2307.03172"},
            "url": "https://www.semanticscholar.org/paper/abc123",
            "abstract": "While recent language models have the ability to take long contexts "
            "as input, relatively little is known about how well they use longer context.",
        }
    ],
}

CROSSREF_DOI_PAYLOAD = {
    "status": "ok",
    "message": {
        "DOI": "10.48550/arxiv.2307.03172",
        "title": ["Lost in the Middle: How Language Models Use Long Contexts"],
        "issued": {"date-parts": [[2023, 7]]},
        "author": [{"given": "Nelson F.", "family": "Liu"}],
        "URL": "http://dx.doi.org/10.48550/arxiv.2307.03172",
    },
}

OPENALEX_PAYLOAD = {
    "results": [
        {
            "id": "https://openalex.org/W4385245566",
            "display_name": "A Broad-Coverage Fallback Paper",
            "publication_year": 2022,
            "doi": "https://doi.org/10.9999/fallback.1",
            "authorships": [{"author": {"display_name": "Ada Fallback"}}],
            "ids": {"openalex": "https://openalex.org/W4385245566"},
            "abstract_inverted_index": {"Hello": [0], "corpus": [1], "world": [2]},
        }
    ]
}


def _transport(
    calls: list[str],
    *,
    s2: httpx.Response | None = None,
    crossref: httpx.Response | None = None,
    openalex: httpx.Response | None = None,
) -> httpx.MockTransport:
    """Route by host; record every request URL. Unrouted host = test bug, fail loud."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        host = request.url.host
        if host == "api.semanticscholar.org" and s2 is not None:
            return s2
        if host == "api.crossref.org" and crossref is not None:
            return crossref
        if host == "api.openalex.org" and openalex is not None:
            return openalex
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=transport)


def _hosts(calls: list[str]) -> list[str]:
    return [httpx.URL(url).host for url in calls]


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------


def test_s2_search_parses_hits() -> None:
    calls: list[str] = []
    client = _client(_transport(calls, s2=httpx.Response(200, json=S2_PAYLOAD)))
    hits = resolve.search_semantic_scholar("lost in the middle", client=client)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.source == "semantic_scholar"
    assert hit.title.startswith("Lost in the Middle")
    assert hit.year == 2023
    assert hit.authors == ("Nelson F. Liu", "Percy Liang")
    assert hit.doi == "10.48550/arxiv.2307.03172"  # normalized: lowercased
    assert hit.arxiv_id == "2307.03172"
    assert hit.url == "https://www.semanticscholar.org/paper/abc123"
    assert hit.abstract_snippet is not None and len(hit.abstract_snippet) <= 300
    assert hit.raw["paperId"] == "abc123"  # the API's own JSON echo rides along


def test_s2_abstract_snippet_truncates_at_the_300_char_boundary() -> None:
    """A 301-char abstract must come back as exactly its 300-char prefix — proving the
    flat[:300] boundary actually fires (a short fixture passes `<= 300` trivially)."""
    long_abstract = "x" * 301
    payload = json.loads(json.dumps(S2_PAYLOAD))
    payload["data"][0]["abstract"] = long_abstract
    calls: list[str] = []
    client = _client(_transport(calls, s2=httpx.Response(200, json=payload)))
    (hit,) = resolve.search_semantic_scholar("q", client=client)
    assert hit.abstract_snippet is not None
    assert len(hit.abstract_snippet) == 300
    assert hit.abstract_snippet == long_abstract[:300]


def test_s2_key_header_sent_only_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("x-api-key"))
        return httpx.Response(200, json={"total": 0, "data": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolve.search_semantic_scholar("q", client=client)
    monkeypatch.setenv(resolve.S2_KEY_ENV, "sekrit")
    resolve.search_semantic_scholar("q", client=client)
    assert seen_headers == [None, "sekrit"]


def test_s2_429_retries_respect_retry_after_then_succeeds() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=S2_PAYLOAD)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hits = resolve.search_semantic_scholar("q", client=client, sleep=sleeps.append)
    assert len(hits) == 1
    assert sleeps == [3.0, 3.0]  # paced by the live Retry-After header, not a guess


def test_s2_429_backoff_without_retry_after_is_exponential() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"total": 0, "data": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolve.search_semantic_scholar("q", client=client, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]


def _429_then_ok_client(retry_after: str) -> httpx.Client:
    """Two 429s carrying a hostile Retry-After value, then a clean 200."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"total": 0, "data": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_s2_429_negative_retry_after_clamps_to_zero_not_crash() -> None:
    """Retry-After: -1 crashed pre-fix (time.sleep ValueError); now clamps to 0."""
    sleeps: list[float] = []
    with _429_then_ok_client("-1") as client:
        resolve.search_semantic_scholar("q", client=client, sleep=sleeps.append)
    assert sleeps == [0.0, 0.0]


def test_s2_429_huge_retry_after_clamps_to_cap() -> None:
    """A trillion-second Retry-After must wait only the cap, never hang the process."""
    sleeps: list[float] = []
    with _429_then_ok_client("1000000000000") as client:
        resolve.search_semantic_scholar("q", client=client, sleep=sleeps.append)
    assert sleeps == [resolve.RETRY_AFTER_CAP_S, resolve.RETRY_AFTER_CAP_S]


def test_s2_429_garbage_retry_after_falls_back_to_backoff() -> None:
    sleeps: list[float] = []
    with _429_then_ok_client("tomorrow-ish") as client:
        resolve.search_semantic_scholar("q", client=client, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]  # the caller's own exponential schedule


def test_s2_429_http_date_retry_after_in_the_past_clamps_to_zero() -> None:
    sleeps: list[float] = []
    with _429_then_ok_client("Wed, 21 Oct 2015 07:28:00 GMT") as client:
        resolve.search_semantic_scholar("q", client=client, sleep=sleeps.append)
    assert sleeps == [0.0, 0.0]


def test_parse_retry_after_http_date_forms() -> None:
    """HTTP-date Retry-After: delay = date - now, clamped to [0, RETRY_AFTER_CAP_S]."""
    fallback = 9.0
    near = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    delay = resolve._parse_retry_after(near, fallback)
    assert 20.0 <= delay <= 31.0  # ~30 s out, generous skew tolerance
    far = format_datetime(datetime.now(UTC) + timedelta(days=30))
    assert resolve._parse_retry_after(far, fallback) == resolve.RETRY_AFTER_CAP_S
    past = format_datetime(datetime.now(UTC) - timedelta(days=1))
    assert resolve._parse_retry_after(past, fallback) == 0.0
    assert resolve._parse_retry_after(None, fallback) == fallback
    assert resolve._parse_retry_after("not a date", fallback) == fallback


def test_s2_429_is_bounded_then_raises() -> None:
    sleeps: list[float] = []
    calls: list[str] = []
    client = _client(_transport(calls, s2=httpx.Response(429, headers={"Retry-After": "1"})))
    with pytest.raises(resolve.ResolutionError, match="rate-limiting"):
        resolve.search_semantic_scholar("q", client=client, sleep=sleeps.append)
    assert len(calls) == resolve.S2_MAX_ATTEMPTS  # bounded — never an infinite retry loop
    assert len(sleeps) == resolve.S2_MAX_ATTEMPTS - 1


def test_s2_http_error_raises_loud() -> None:
    calls: list[str] = []
    client = _client(_transport(calls, s2=httpx.Response(500)))
    with pytest.raises(resolve.ResolutionError, match="HTTP 500"):
        resolve.search_semantic_scholar("q", client=client)


# ---------------------------------------------------------------------------
# Crossref — header-driven throttle
# ---------------------------------------------------------------------------


def test_crossref_throttle_paces_from_live_headers() -> None:
    """Second call's spacing is derived from the FIRST response's headers: 10s/50 = 0.2s."""
    clock = _FakeClock()
    throttle = resolve.CrossrefThrottle(clock=clock.monotonic, sleep=clock.sleep)
    calls: list[str] = []
    response = httpx.Response(
        200,
        json=CROSSREF_DOI_PAYLOAD,
        headers={"X-Rate-Limit-Limit": "50", "X-Rate-Limit-Interval": "10s"},
    )
    client = _client(_transport(calls, crossref=response))
    resolve.lookup_crossref_doi("10.48550/arXiv.2307.03172", client=client, throttle=throttle)
    assert clock.sleeps == []  # first call is never delayed
    assert throttle.header_derived is True
    assert throttle.min_interval_s == pytest.approx(0.2)
    resolve.lookup_crossref_doi("10.48550/arXiv.2307.03172", client=client, throttle=throttle)
    assert clock.sleeps == [pytest.approx(0.2)]  # pacing came from the mocked headers


def test_crossref_throttle_header_absent_falls_back_conservative() -> None:
    clock = _FakeClock()
    throttle = resolve.CrossrefThrottle(clock=clock.monotonic, sleep=clock.sleep)
    calls: list[str] = []
    client = _client(_transport(calls, crossref=httpx.Response(200, json=CROSSREF_DOI_PAYLOAD)))
    resolve.lookup_crossref_doi("10.1/x", client=client, throttle=throttle)
    resolve.lookup_crossref_doi("10.1/x", client=client, throttle=throttle)
    assert throttle.header_derived is False
    # The 1 req/s number is the documented header-absent fallback, not an API claim.
    assert clock.sleeps == [pytest.approx(resolve.CrossrefThrottle.FALLBACK_MIN_INTERVAL_S)]


def test_crossref_doi_lookup_normalizes_and_sends_mailto() -> None:
    calls: list[str] = []
    client = _client(_transport(calls, crossref=httpx.Response(200, json=CROSSREF_DOI_PAYLOAD)))
    message = resolve.lookup_crossref_doi(
        "https://doi.org/10.48550/arXiv.2307.03172", client=client, throttle=_noop_throttle()
    )
    assert message["DOI"] == "10.48550/arxiv.2307.03172"
    (url,) = calls
    assert "/works/10.48550/arxiv.2307.03172" in url
    assert f"mailto={resolve.DEFAULT_CROSSREF_MAILTO}" in url.replace("%40", "@")


def test_crossref_mailto_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(resolve.CROSSREF_MAILTO_ENV, "operator@real.example")
    calls: list[str] = []
    client = _client(_transport(calls, crossref=httpx.Response(200, json=CROSSREF_DOI_PAYLOAD)))
    resolve.lookup_crossref_doi("10.1/x", client=client, throttle=_noop_throttle())
    assert "operator@real.example" in calls[0].replace("%40", "@")


def test_crossref_404_raises_loud() -> None:
    calls: list[str] = []
    client = _client(_transport(calls, crossref=httpx.Response(404)))
    with pytest.raises(resolve.ResolutionError, match="HTTP 404"):
        resolve.lookup_crossref_doi(
            "10.48550/arxiv.2307.03172", client=client, throttle=_noop_throttle()
        )


def test_search_crossref_parses_items() -> None:
    payload = {"status": "ok", "message": {"items": [CROSSREF_DOI_PAYLOAD["message"]]}}
    calls: list[str] = []
    client = _client(_transport(calls, crossref=httpx.Response(200, json=payload)))
    hits = resolve.search_crossref("lost in the middle", client=client, throttle=_noop_throttle())
    assert len(hits) == 1
    assert hits[0].source == "crossref"
    assert hits[0].doi == "10.48550/arxiv.2307.03172"
    assert hits[0].year == 2023
    assert hits[0].authors == ("Nelson F. Liu",)


# ---------------------------------------------------------------------------
# OpenAlex — key gate
# ---------------------------------------------------------------------------


def test_openalex_key_missing_raises_loud_and_fetches_nothing() -> None:
    calls: list[str] = []
    client = _client(_transport(calls, openalex=httpx.Response(200, json=OPENALEX_PAYLOAD)))
    with pytest.raises(resolve.OpenAlexKeyMissing) as excinfo:
        resolve.search_openalex("anything", client=client)
    message = str(excinfo.value)
    assert resolve.OPENALEX_KEY_ENV in message  # actionable: names the env var
    assert resolve.OPENALEX_SIGNUP_URL in message  # actionable: names the signup URL
    assert calls == []  # the gate fires BEFORE any request


def test_openalex_search_parses_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(resolve.OPENALEX_KEY_ENV, "oa-key")
    calls: list[str] = []
    client = _client(_transport(calls, openalex=httpx.Response(200, json=OPENALEX_PAYLOAD)))
    hits = resolve.search_openalex("fallback paper", client=client)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.source == "openalex"
    assert hit.title == "A Broad-Coverage Fallback Paper"
    assert hit.doi == "10.9999/fallback.1"
    assert hit.authors == ("Ada Fallback",)
    assert hit.abstract_snippet == "Hello corpus world"
    # OpenAlex offers no header-based auth form — the key travels as a query param
    # (S2's key uses the x-api-key header); leakage is guarded by the redaction test.
    assert "api_key=oa-key" in calls[0]


def test_openalex_error_text_never_contains_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport error whose message embeds the full request URL (the worst case —
    the query string carries api_key) must surface REDACTED: the ResolutionError that
    reaches CLI output / notes never contains the key."""
    monkeypatch.setenv(resolve.OPENALEX_KEY_ENV, "oa-key-hunter2")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"simulated failure connecting to {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(resolve.ResolutionError) as excinfo:
        resolve.search_openalex("anything", client=client)
    message = str(excinfo.value)
    assert "oa-key-hunter2" not in message
    assert "<redacted>" in message


# ---------------------------------------------------------------------------
# Tiered orchestrator
# ---------------------------------------------------------------------------


def test_tiered_s2_hit_canonicalizes_via_crossref_never_openalex() -> None:
    calls: list[str] = []
    client = _client(
        _transport(
            calls,
            s2=httpx.Response(200, json=S2_PAYLOAD),
            crossref=httpx.Response(200, json=CROSSREF_DOI_PAYLOAD),
        )
    )
    result = resolve.resolve_citation(
        "lost in the middle", client=client, throttle=_noop_throttle()
    )
    assert result.resolved is True
    assert result.tier == "semantic_scholar"
    assert result.crossref_echo is not None
    assert result.crossref_echo["DOI"] == "10.48550/arxiv.2307.03172"
    assert _hosts(calls) == ["api.semanticscholar.org", "api.crossref.org"]
    assert result.tiers_tried == ("semantic_scholar", "crossref")
    record = result.resolution_record()
    assert record["api_echo"] is not None and record["api_echo"]["paperId"] == "abc123"


def test_tiered_s2_miss_falls_back_to_openalex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(resolve.OPENALEX_KEY_ENV, "oa-key")
    calls: list[str] = []
    client = _client(
        _transport(
            calls,
            s2=httpx.Response(200, json={"total": 0, "data": []}),
            openalex=httpx.Response(200, json=OPENALEX_PAYLOAD),
            crossref=httpx.Response(200, json=CROSSREF_DOI_PAYLOAD),
        )
    )
    result = resolve.resolve_citation("obscure paper", client=client, throttle=_noop_throttle())
    assert result.resolved is True
    assert result.tier == "openalex"
    # The OpenAlex hit carries a DOI, so canonicalization still runs.
    assert _hosts(calls) == ["api.semanticscholar.org", "api.openalex.org", "api.crossref.org"]


def test_tiered_s2_error_degrades_with_note_and_still_reaches_openalex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An S2 ERROR (HTTP 500, not a miss) inside resolve_citation degrades to a
    'semantic_scholar error:' note and the OpenAlex tier still resolves the query."""
    monkeypatch.setenv(resolve.OPENALEX_KEY_ENV, "oa-key")
    calls: list[str] = []
    client = _client(
        _transport(
            calls,
            s2=httpx.Response(500),
            openalex=httpx.Response(200, json=OPENALEX_PAYLOAD),
            crossref=httpx.Response(200, json=CROSSREF_DOI_PAYLOAD),
        )
    )
    result = resolve.resolve_citation("flaky upstream", client=client, throttle=_noop_throttle())
    assert result.resolved is True
    assert result.tier == "openalex"
    assert any(note.startswith("semantic_scholar error:") for note in result.notes)
    assert _hosts(calls)[:2] == ["api.semanticscholar.org", "api.openalex.org"]


def test_tiered_both_s2_and_openalex_error_raises_not_quiet_no_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both tiers down is an ERROR, never a quiet resolved=False."""
    monkeypatch.setenv(resolve.OPENALEX_KEY_ENV, "oa-key")
    calls: list[str] = []
    client = _client(_transport(calls, s2=httpx.Response(500), openalex=httpx.Response(500)))
    with pytest.raises(resolve.ResolutionError, match=r"openalex\.org.* returned HTTP 500"):
        resolve.resolve_citation("everything down", client=client, throttle=_noop_throttle())
    assert _hosts(calls) == ["api.semanticscholar.org", "api.openalex.org"]  # both tried


def test_tiered_s2_miss_without_openalex_key_raises_loud() -> None:
    calls: list[str] = []
    client = _client(_transport(calls, s2=httpx.Response(200, json={"total": 0, "data": []})))
    with pytest.raises(resolve.OpenAlexKeyMissing):
        resolve.resolve_citation("obscure paper", client=client, throttle=_noop_throttle())
    assert _hosts(calls) == ["api.semanticscholar.org"]  # OpenAlex tier reached, nothing fetched


def test_tiered_no_hits_anywhere_is_first_class_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(resolve.OPENALEX_KEY_ENV, "oa-key")
    calls: list[str] = []
    client = _client(
        _transport(
            calls,
            s2=httpx.Response(200, json={"total": 0, "data": []}),
            openalex=httpx.Response(200, json={"results": []}),
        )
    )
    result = resolve.resolve_citation("nothing anywhere", client=client, throttle=_noop_throttle())
    assert result.resolved is False
    assert result.hit is None
    assert result.tiers_tried == ("semantic_scholar", "openalex")


def test_tiered_crossref_canonicalization_failure_keeps_hit_with_note() -> None:
    """DataCite/arXiv DOIs 404 on Crossref — the S2 echo stands; the failure is recorded."""
    calls: list[str] = []
    client = _client(
        _transport(
            calls,
            s2=httpx.Response(200, json=S2_PAYLOAD),
            crossref=httpx.Response(404),
        )
    )
    result = resolve.resolve_citation(
        "lost in the middle", client=client, throttle=_noop_throttle()
    )
    assert result.resolved is True
    assert result.crossref_echo is None
    assert any("crossref canonicalization failed" in note for note in result.notes)


def test_normalize_doi_variants() -> None:
    for raw in (
        "10.48550/arXiv.2307.03172",
        "https://doi.org/10.48550/arXiv.2307.03172",
        "http://dx.doi.org/10.48550/ARXIV.2307.03172",
        "doi:10.48550/arxiv.2307.03172",
        "  10.48550/arxiv.2307.03172/  ",
    ):
        assert resolve.normalize_doi(raw) == "10.48550/arxiv.2307.03172"


def test_default_timeout_has_one_source_of_truth() -> None:
    """`is`, not `==`: resolve.py and verify.py must share the ONE _http.DEFAULT_TIMEOUT
    object — a re-duplicated identical-looking constant fails this (code-quality.md
    § one source of truth for data-shape constants)."""
    assert resolve.DEFAULT_TIMEOUT is _http.DEFAULT_TIMEOUT
    assert verify.DEFAULT_TIMEOUT is _http.DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# CLI integration (production entry point; transport injected via _build_client)
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_db(tmp_path: Path) -> Path:
    path = tmp_path / "cite.db"
    assert main(["init-db", "--db", str(path)]) == 0
    return path


def _patch_cli_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    monkeypatch.setattr(resolve, "_build_client", lambda: httpx.Client(transport=transport))
    monkeypatch.setattr(resolve, "_default_throttle", _noop_throttle())


def test_cli_resolve_prints_result_and_writes_nothing(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    _patch_cli_transport(
        monkeypatch,
        _transport(
            calls,
            s2=httpx.Response(200, json=S2_PAYLOAD),
            crossref=httpx.Response(200, json=CROSSREF_DOI_PAYLOAD),
        ),
    )
    exit_code = main(["resolve", "lost in the middle long contexts", "--db", str(cli_db)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Tier: semantic_scholar" in out
    assert "Lost in the Middle" in out
    assert "DOI: 10.48550/arxiv.2307.03172" in out
    assert "Read-only: nothing was written." in out
    conn = sqlite3.connect(str(cli_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0] == 0
    finally:
        conn.close()


def test_cli_resolve_openalex_key_missing_is_a_clean_error(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    _patch_cli_transport(
        monkeypatch, _transport(calls, s2=httpx.Response(200, json={"total": 0, "data": []}))
    )
    exit_code = main(["resolve", "obscure paper", "--db", str(cli_db)])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "error:" in out
    assert resolve.OPENALEX_KEY_ENV in out


def test_cli_resolve_without_db_still_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    _patch_cli_transport(
        monkeypatch,
        _transport(
            calls,
            s2=httpx.Response(200, json=S2_PAYLOAD),
            crossref=httpx.Response(200, json=CROSSREF_DOI_PAYLOAD),
        ),
    )
    exit_code = main(["resolve", "lost in the middle", "--db", str(tmp_path / "missing.db")])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "database not found" in out
    assert "Tier: semantic_scholar" in out


# ---------------------------------------------------------------------------
# Live smoke (excluded by default; run once via `uv run pytest -m live`)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_tiered_resolution_of_arxiv_2307_03172() -> None:
    """arXiv:2307.03172 resolves via the real tiered path (S2 first).

    Loud on real failures; transient upstream trouble (shared-pool 429 exhaustion,
    5xx, network) becomes an explicit skip with the reason — never a false green.
    Independent of the Crossref live test below so one upstream outage cannot hide
    the other's evidence.
    """
    try:
        result = resolve.resolve_citation(
            "Lost in the Middle: How Language Models Use Long Contexts",
            throttle=resolve.CrossrefThrottle(),
        )
    except resolve.OpenAlexKeyMissing as exc:
        pytest.skip(f"S2 missed/failed live and no OpenAlex key is configured: {exc}")
    except resolve.ResolutionError as exc:
        pytest.skip(f"transient upstream failure during tiered resolution: {exc}")
    assert result.resolved, f"expected a live hit; tiers tried: {result.tiers_tried}"
    assert result.hit is not None
    assert result.hit.doi == "10.48550/arxiv.2307.03172" or result.hit.arxiv_id == "2307.03172", (
        f"unexpected identifiers: doi={result.hit.doi!r} arxiv={result.hit.arxiv_id!r}"
    )
    assert result.hit.raw, "resolution record must carry the API's own JSON echo"


@pytest.mark.live
def test_live_crossref_call_throttles_from_response_headers() -> None:
    """A real Crossref call returns rate-limit headers the throttle reads.

    Uses a Crossref-registered DOI (the arXiv DOI is DataCite-registered and
    legitimately 404s on Crossref). Transient failure = explicit skip; header
    absence = explicit skip naming the upstream contract drift — never false green.
    """
    throttle = resolve.CrossrefThrottle()
    try:
        echo = resolve.lookup_crossref_doi("10.1145/3442188.3445922", throttle=throttle)
    except resolve.ResolutionError as exc:
        pytest.skip(f"transient Crossref failure: {exc}")
    assert isinstance(echo.get("DOI"), str)
    assert throttle.observed_count >= 1
    if not throttle.header_derived:
        pytest.skip(
            "Crossref response carried no X-Rate-Limit-* headers — the conservative "
            "1 req/s fallback engaged. Surfacing loudly instead of asserting: the "
            "header contract appears to have drifted upstream."
        )
    assert throttle.min_interval_s > 0
    print(
        "live throttle: "
        + json.dumps(
            {
                "min_interval_s": throttle.min_interval_s,
                "header_derived": throttle.header_derived,
                "observed_count": throttle.observed_count,
            }
        )
    )
