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
csvio — read a lead CSV (only `email` required; every other column rides through
untouched) and write the segmenter's outputs: one CSV per planned segment, an MX
audit rolled up by domain, and a manifest.json describing the whole plan.

The segment CSVs carry all of your original columns plus `mx_provider` / `mx_host` /
`mx_error`, so whatever copy or fields you already had (subjects, bodies, personas)
survive the split and go straight into your sequencer.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path

from . import classify

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Accepted header spellings for the two columns we actually reason about.
EMAIL_ALIASES = ("email", "email_address", "emailaddress", "e-mail", "work_email")
COMPANY_ALIASES = ("company", "company_name", "organization", "org", "account", "institution")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _resolve_column(fieldnames: list[str], aliases: tuple[str, ...]) -> str | None:
    norm = {_norm(f): f for f in fieldnames}
    for a in aliases:
        if _norm(a) in norm:
            return norm[_norm(a)]
    return None


class CsvError(Exception):
    pass


def read_leads(path: str) -> dict:
    """
    Parse a CSV into deduped lead rows. Returns:
      {"rows": [<raw dict>, ...], "fieldnames": [...],
       "email_col": str, "company_col": str|None,
       "dropped_bad_email": int, "dropped_duplicate": int}
    Rows with a missing/invalid email are dropped; the first row wins on duplicate email.
    """
    p = Path(path)
    if not p.exists():
        raise CsvError(f"CSV not found: {path}")
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise CsvError("CSV has no header row")
        email_col = _resolve_column(reader.fieldnames, EMAIL_ALIASES)
        company_col = _resolve_column(reader.fieldnames, COMPANY_ALIASES)
        if not email_col:
            raise CsvError(
                f"CSV needs an email column (one of {EMAIL_ALIASES}). "
                f"Header seen: {reader.fieldnames}"
            )
        rows, seen = [], set()
        dropped_bad, dropped_dupe = 0, 0
        for raw in reader:
            email = (raw.get(email_col) or "").strip().lower()
            if not email or not EMAIL_RE.match(email):
                dropped_bad += 1
                continue
            if email in seen:
                dropped_dupe += 1
                continue
            seen.add(email)
            rows.append(raw)
    return {
        "rows": rows, "fieldnames": list(reader.fieldnames),
        "email_col": email_col, "company_col": company_col,
        "dropped_bad_email": dropped_bad, "dropped_duplicate": dropped_dupe,
    }


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "run"


def write_segment_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_mx_audit(path: Path, items: list[dict]) -> None:
    """Roll the classification up by domain: which gateway, which MX host, how many leads."""
    agg: dict[str, dict] = {}
    for it in items:
        dom = classify.domain_of(it["email"])
        a = agg.setdefault(dom, {"domain": dom, "provider": it["provider"],
                                 "mx_host": it.get("mx_host", ""),
                                 "mx_error": it.get("mx_error", ""), "leads": 0})
        a["leads"] += 1
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["domain", "provider", "mx_host", "mx_error", "leads"])
        w.writeheader()
        for dom in sorted(agg, key=lambda d: (-agg[d]["leads"], d)):
            w.writerow(agg[dom])


def write_outputs(plan: dict, *, out_dir: Path, fieldnames: list[str],
                  items: list[dict]) -> dict:
    """
    Write every segment CSV + the MX audit + manifest.json under out_dir. Returns the
    manifest dict (also written to disk). Segment `leads` (the full row objects) are
    stripped from the manifest — the CSVs hold the rows; the manifest holds the plan.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ensure the classification columns are present in the output header
    out_fields = list(fieldnames)
    for extra in ("mx_provider", "mx_host", "mx_error"):
        if extra not in out_fields:
            out_fields.append(extra)

    manifest = {
        "base_name": plan["base_name"],
        "created_at": date.today().isoformat(),
        "gap_days": plan["gap_days"],
        "first_wait": plan["first_wait"],
        "drip_per_day": plan["drip_per_day"],
        "out_dir": str(out_dir),
        "distribution": plan["distribution"],
        "segments": [],
    }

    for seg in plan["segments"]:
        fname = f"{seg['provider']}_{seg['kind']}.csv"
        csv_path = out_dir / fname
        write_segment_csv(csv_path, out_fields, [it["row"] for it in seg["leads"]])
        entry = {k: v for k, v in seg.items() if k != "leads"}
        entry["csv"] = str(csv_path)
        manifest["segments"].append(entry)

    audit_path = out_dir / "mx_audit.csv"
    write_mx_audit(audit_path, items)
    manifest["mx_audit_csv"] = str(audit_path)

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    manifest["manifest_path"] = str(manifest_path)
    return manifest
