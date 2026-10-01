# Copyright 2026 Kai Karlstrom
# SPDX-License-Identifier: Apache-2.0
"""Shared test fixtures — a fake DNS resolver so the suite never touches the network."""
from __future__ import annotations

import dns.resolver
import pytest


class _MX:
    def __init__(self, preference: int, exchange: str):
        self.preference = preference
        self.exchange = exchange


class FakeResolver:
    """
    Mimics dns.resolver.Resolver just enough for classify._resolve_mx.
    `records` maps domain -> list[(preference, exchange)]. A domain mapped to the
    sentinel "NXDOMAIN" raises NXDOMAIN; anything absent raises NoAnswer.
    """
    def __init__(self, records: dict):
        self.records = records
        self.timeout = 5
        self.lifetime = 10
        self.nameservers = ["1.1.1.1"]

    def resolve(self, domain, rdtype):
        val = self.records.get(domain)
        if val == "NXDOMAIN":
            raise dns.resolver.NXDOMAIN
        if val is None:
            raise dns.resolver.NoAnswer
        return [_MX(pref, host) for pref, host in val]


@pytest.fixture
def fake_records():
    return {
        # a school fronted by Proofpoint but hosted on M365 — must bucket to proofpoint
        "cityschools.example": [(10, "mx1.pphosted.com."), (10, "cityschools-example.mail.protection.outlook.com.")],
        "county.example":      [(5, "county-example.mail.protection.outlook.com.")],
        "district.example":    [(10, "us-smtp-inbound-1.mimecast.com."), (20, "us-smtp-inbound-2.mimecast.com.")],
        "township.example":    [(5, "cust01234.ess.barracudanetworks.com.")],
        # Sophos listed beside an Outlook MX — the gateway filters inbound, so it must win
        "village.example":     [(0, "village-example.mail.protection.outlook.com."), (10, "mx-01-us-east-2.prod.hydra.sophos.com.")],
        "startup.example":     [(1, "aspmx.l.google.com."), (5, "alt1.aspmx.l.google.com.")],
        "misc.example":        [(10, "mail.misc.example.")],
        "gone.example":        "NXDOMAIN",
    }


@pytest.fixture
def resolver(fake_records):
    return FakeResolver(fake_records)
