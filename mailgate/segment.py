# Copyright 2026 Kai Karlstrom
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
segment — turn a gateway-classified lead list into a deliverability-safe send plan.

Four best practices, encoded as data (no sending happens here — this produces a plan
and per-segment CSVs you feed to whatever sequencer you use):

  1. Isolate each gateway into its own segment, so a gateway-tripping send to one
     provider can't taint your reputation with the others.
  2. Per-company throttle. Within a gateway, Wave 1 gets AT MOST ONE lead per company;
     the rest go to a Drip segment that trickles in a few new leads per day. Hitting
     ten inboxes at one company on day one is the fastest way to a domain block.
  3. Round-robin the drip across companies, so consecutive daily adds hit DIFFERENT
     companies instead of walking one company's directory top to bottom.
  4. Sender policy per gateway: protected gateways (Proofpoint/Mimecast/Barracuda) are
     tagged `microsoft_only` (Microsoft-to-Microsoft is the most trusted path through
     them); everything else is `microsoft_preferred`. Guidance carried in the manifest.

The wave/drip split is deterministic: which lead becomes a company's Wave 1 pick is
chosen by a stable hash of the email, so re-running the same list produces the same
plan.
"""
from __future__ import annotations

import hashlib
import re

from . import classify

# Protected gateways get the strictest handling. Ordering here is display order.
PROTECTED = classify.PROTECTED
PROVIDER_ORDER = PROTECTED + ("microsoft", "google", "other", "unknown")
PROVIDER_LABEL = {
    "proofpoint": "Proofpoint", "mimecast": "Mimecast", "barracuda": "Barracuda",
    "microsoft": "Microsoft", "google": "Google", "other": "Other", "unknown": "Unknown",
}

# Role/shared inboxes (info@, board@, ...) — worth flagging: they behave differently
# from a named person's inbox and often route straight to a gateway quarantine.
ROLE_LOCALPART_RE = re.compile(
    r"^(info|admin|office|clerk|board|contact|hello|help|support|webmaster|postmaster|sales|team|no-?reply)\b",
    re.I,
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _email_rank_key(email: str) -> str:
    """Stable per-email hash — decides which lead is a company's Wave 1 representative."""
    return hashlib.sha1(email.strip().lower().encode()).hexdigest()


def is_role_inbox(email: str) -> bool:
    local = (email or "").split("@", 1)[0]
    return bool(ROLE_LOCALPART_RE.match(local))


def company_key(row: dict, mode: str = "company_or_domain",
                company_field: str = "company", email_field: str = "email") -> str:
    """
    The per-company throttle key. `company` uses the company column (falling back to
    the email domain when blank); `domain` always uses the email domain; the default
    `company_or_domain` prefers company and falls back to domain.
    """
    company = _norm(row.get(company_field, ""))
    dom = classify.domain_of(row.get(email_field, ""))
    if mode == "domain":
        return f"@{dom}"
    if mode == "company":
        return company or f"@{dom}"
    return company or f"@{dom}"  # company_or_domain (default)


def segment(items: list[dict]) -> dict:
    """
    items: [{"row": <original dict>, "provider": str, "company_key": str}, ...]
           (already deduped by email).

    Returns {provider: {"wave1": [item,...], "drip": [item,...],
                        "companies": int, "total": int}}
    with the drip round-robin ordered by company.
    """
    by_provider: dict[str, list[dict]] = {}
    for it in items:
        by_provider.setdefault(it["provider"], []).append(it)

    out = {}
    for provider, group in by_provider.items():
        companies: dict[str, list[dict]] = {}
        for it in group:
            companies.setdefault(it["company_key"], []).append(it)

        wave1, drip_by_company = [], {}
        for ck, leads in companies.items():
            leads_sorted = sorted(leads, key=lambda x: _email_rank_key(x["email"]))
            wave1.append(leads_sorted[0])
            if len(leads_sorted) > 1:
                drip_by_company[ck] = leads_sorted[1:]

        # round-robin the drip across companies so consecutive daily adds hit different cos
        drip = []
        if drip_by_company:
            company_order = sorted(drip_by_company)
            maxlen = max(len(v) for v in drip_by_company.values())
            for i in range(maxlen):
                for ck in company_order:
                    if i < len(drip_by_company[ck]):
                        drip.append(drip_by_company[ck][i])

        out[provider] = {"wave1": wave1, "drip": drip,
                         "companies": len(companies), "total": len(group)}
    return out


def sender_policy(provider: str) -> str:
    return "microsoft_only" if provider in PROTECTED else "microsoft_preferred"


def build_plan(items: list[dict], *, base_name: str, gap_days: int = 5,
               first_wait: int = 1, drip_per_day: int = 5) -> dict:
    """
    Assemble the full send plan: one Wave 1 + one Drip segment per gateway that has
    leads, each annotated with the recommended cadence + sender policy. Pure data —
    no network, no sending.
    """
    seg = segment(items)
    plan = {
        "base_name": base_name,
        "gap_days": gap_days,
        "first_wait": first_wait,
        "drip_per_day": drip_per_day,
        "segments": [],
        "distribution": {},
    }
    for provider in PROVIDER_ORDER:
        if provider not in seg:
            continue
        s = seg[provider]
        plan["distribution"][provider] = {
            "total": s["total"], "wave1": len(s["wave1"]),
            "drip": len(s["drip"]), "companies": s["companies"],
        }
        for kind, kind_label, leads in (("wave1", "Wave 1", s["wave1"]),
                                        ("drip", "Drip", s["drip"])):
            if not leads:
                continue
            entry = {
                "name": f"{base_name} · {PROVIDER_LABEL[provider]} · {kind_label}",
                "provider": provider,
                "kind": kind,
                "sender_policy": sender_policy(provider),
                "protected": provider in PROTECTED,
                "lead_count": len(leads),
                "company_count": len({it["company_key"] for it in leads}),
                "role_inbox_count": sum(1 for it in leads if is_role_inbox(it["email"])),
                "step_gap_days": gap_days,
                "first_wait_days": first_wait,
                "leads": leads,
            }
            if kind == "drip":
                entry["drip_per_day"] = drip_per_day
            plan["segments"].append(entry)
    return plan
