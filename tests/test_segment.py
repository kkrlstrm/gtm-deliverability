# Copyright 2026 Kai Karlstrom
# SPDX-License-Identifier: Apache-2.0
"""The wave/drip throttle is pure logic — no network needed here at all."""
from __future__ import annotations

from mailgate import segment


def _items(pairs):
    """pairs: [(email, provider, company_key), ...] -> segmenter items."""
    return [{"row": {"email": e}, "email": e, "provider": p, "company_key": c}
            for e, p, c in pairs]


def test_wave1_is_at_most_one_lead_per_company():
    items = _items([
        ("a@acme.example", "proofpoint", "acme"),
        ("b@acme.example", "proofpoint", "acme"),
        ("c@acme.example", "proofpoint", "acme"),
        ("d@beta.example", "proofpoint", "beta"),
    ])
    seg = segment.segment(items)["proofpoint"]
    assert len(seg["wave1"]) == 2          # one per company: acme + beta
    assert len(seg["drip"]) == 2           # the two extra acme leads
    assert seg["companies"] == 2
    # every company appears exactly once in wave1
    assert len({it["company_key"] for it in seg["wave1"]}) == 2


def test_gateways_are_isolated():
    items = _items([
        ("a@acme.example", "proofpoint", "acme"),
        ("b@beta.example", "mimecast", "beta"),
        ("c@gamma.example", "google", "gamma"),
    ])
    seg = segment.segment(items)
    assert set(seg) == {"proofpoint", "mimecast", "google"}
    assert seg["proofpoint"]["total"] == 1


def test_drip_is_round_robined_across_companies():
    # two companies, three extra leads each — drip must alternate companies, not
    # walk one company's directory top to bottom.
    items = _items(
        [(f"a{i}@acme.example", "mimecast", "acme") for i in range(4)]
        + [(f"b{i}@beta.example", "mimecast", "beta") for i in range(4)]
    )
    seg = segment.segment(items)["mimecast"]
    drip_companies = [it["company_key"] for it in seg["drip"]]
    # no two consecutive drip adds hit the same company
    assert all(a != b for a, b in zip(drip_companies, drip_companies[1:]))


def test_plan_is_deterministic():
    items = _items([
        ("a@acme.example", "barracuda", "acme"),
        ("b@acme.example", "barracuda", "acme"),
    ])
    p1 = segment.build_plan(items, base_name="X")
    p2 = segment.build_plan(items, base_name="X")
    names1 = [s["name"] for s in p1["segments"]]
    names2 = [s["name"] for s in p2["segments"]]
    assert names1 == names2


def test_sender_policy_and_protected_flag():
    assert segment.sender_policy("proofpoint") == "microsoft_only"
    assert segment.sender_policy("google") == "microsoft_preferred"

    items = _items([("a@acme.example", "proofpoint", "acme"),
                    ("b@beta.example", "google", "beta")])
    plan = segment.build_plan(items, base_name="Q3")
    by_provider = {s["provider"]: s for s in plan["segments"]}
    assert by_provider["proofpoint"]["protected"] is True
    assert by_provider["proofpoint"]["sender_policy"] == "microsoft_only"
    assert by_provider["google"]["protected"] is False


def test_role_inbox_is_flagged():
    items = _items([("info@acme.example", "google", "acme"),
                    ("jane.doe@acme.example", "google", "acme")])
    plan = segment.build_plan(items, base_name="R")
    total_role = sum(s["role_inbox_count"] for s in plan["segments"])
    assert total_role == 1


def test_company_key_falls_back_to_domain():
    assert segment.company_key({"email": "a@acme.example"}, "company_or_domain") == "@acme.example"
    assert segment.company_key({"email": "a@acme.example", "company": "Acme Inc"},
                               "company_or_domain") == "acmeinc"
    assert segment.company_key({"email": "a@acme.example", "company": "Acme Inc"},
                               "domain") == "@acme.example"
