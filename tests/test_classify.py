# Copyright 2026 Kai Karlstrom
# SPDX-License-Identifier: Apache-2.0
"""Classification is deterministic and offline — every test injects the fake resolver."""
from __future__ import annotations

from datetime import date

from mailgate import classify


def _classify(domain, resolver):
    return classify.classify_domain(domain, cache={}, resolver=resolver)["provider"]


def test_protected_gateway_wins_over_microsoft(resolver):
    # cityschools is Proofpoint in front, M365 behind — the gateway is what filters.
    assert _classify("cityschools.example", resolver) == "proofpoint"


def test_mimecast_and_barracuda(resolver):
    assert _classify("district.example", resolver) == "mimecast"
    assert _classify("township.example", resolver) == "barracuda"


def test_sophos_wins_over_microsoft(resolver):
    assert "sophos" in classify.PROTECTED
    assert _classify("village.example", resolver) == "sophos"


def test_stale_cached_label_is_rebucketed():
    """A domain cached as "other" before Sophos was recognised must read as sophos now,
    without a new lookup."""
    host = "mx-01-us-east-2.prod.hydra.sophos.com"
    cache = {"village.example": {
        "domain": "village.example", "provider": "other", "mx_host": host, "pref": 10,
        "all_mx": [f"10:{host}"], "error": "", "resolved_at": date.today().isoformat()}}
    rec = classify.classify_domain("village.example", cache, _Boom())
    assert rec["provider"] == "sophos"
    assert rec["mx_host"] == host


def test_cached_error_is_left_alone():
    rec0 = {"domain": "gone.example", "provider": "unknown", "mx_host": "", "pref": None,
            "all_mx": [], "error": "NXDOMAIN", "resolved_at": date.today().isoformat()}
    rec = classify.classify_domain("gone.example", {"gone.example": rec0}, _Boom())
    assert rec["provider"] == "unknown"


def test_microsoft_and_google(resolver):
    assert _classify("county.example", resolver) == "microsoft"
    assert _classify("startup.example", resolver) == "google"


def test_other_when_mx_resolves_but_matches_nothing(resolver):
    assert _classify("misc.example", resolver) == "other"


def test_unknown_on_nxdomain(resolver):
    rec = classify.classify_domain("gone.example", cache={}, resolver=resolver)
    assert rec["provider"] == "unknown"
    assert rec["error"] == "NXDOMAIN"


def test_bad_domain_is_unknown_without_a_lookup(resolver):
    rec = classify.classify_domain("not-a-domain", cache={}, resolver=resolver)
    assert rec["provider"] == "unknown"
    assert rec["error"] == "bad_domain"


def test_domain_of():
    assert classify.domain_of("Jane.Doe@County.Example ") == "county.example"
    assert classify.domain_of("no-at-sign") == ""


def test_cache_hit_skips_resolver(resolver, fake_records):
    cache = {}
    classify.classify_domain("county.example", cache, resolver)
    # a resolver that would now fail proves the second call is served from cache
    boom = _Boom()
    assert classify.classify_domain("county.example", cache, boom)["provider"] == "microsoft"


def test_classify_rows_annotates_and_dedupes(resolver, tmp_path):
    rows = [
        {"email": "a@district.example"},
        {"email": "b@district.example"},
        {"email": "c@startup.example"},
    ]
    classify.classify_rows(rows, resolver=resolver, qps=0, cache_path=tmp_path / "c.json")
    assert [r["mx_provider"] for r in rows] == ["mimecast", "mimecast", "google"]


class _Boom:
    def resolve(self, *a, **k):
        raise AssertionError("resolver should not be called on a cache hit")
