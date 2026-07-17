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
mailgate — CLI. Two commands, both read-only against your list; neither one sends mail.

    mailgate classify  <emails-or-domains...>   quick one-off gateway lookup
    mailgate segment    leads.csv --name "..."  full pipeline → segmented CSVs + manifest
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import __version__, classify, csvio
from .segment import PROTECTED, PROVIDER_LABEL, PROVIDER_ORDER, build_plan, company_key


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# --------------------------------------------------------------------- classify
def cmd_classify(args) -> int:
    resolver = classify.make_resolver()
    cache = classify.load_cache() if not args.no_cache else {}
    targets = list(args.targets)
    if args.file:
        targets += [ln.strip() for ln in Path(args.file).read_text().splitlines() if ln.strip()]
    if not targets:
        _err("give one or more emails/domains, or --file")
        return 2

    results = []
    for t in targets:
        dom = classify.domain_of(t) if "@" in t else t.strip().lower().strip(".")
        rec = classify.classify_domain(dom, cache, resolver,
                                       refresh=args.refresh, ttl_days=args.ttl_days)
        results.append(rec)
    if not args.no_cache:
        classify.save_cache(cache)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    width = max((len(r["domain"]) for r in results), default=6)
    for r in results:
        tag = "  [PROTECTED]" if r["provider"] in PROTECTED else ""
        host = f"  {r['mx_host']}" if r["mx_host"] else (f"  ({r['error']})" if r["error"] else "")
        print(f"  {r['domain']:<{width}}  {r['provider']:10}{host}{tag}")
    return 0


# ---------------------------------------------------------------------- segment
def cmd_segment(args) -> int:
    try:
        parsed = csvio.read_leads(args.csv)
    except csvio.CsvError as e:
        _err(str(e))
        return 2

    rows = parsed["rows"]
    if not rows:
        _err("no rows with a valid email after parsing")
        return 2
    dropped = parsed["dropped_bad_email"] + parsed["dropped_duplicate"]
    print(f"leads: {len(rows)}"
          + (f"  (dropped {parsed['dropped_bad_email']} bad-email, "
             f"{parsed['dropped_duplicate']} duplicate)" if dropped else ""))
    if not parsed["company_col"]:
        print("note: no company column found — per-company throttle falls back to email domain")

    # classify by receiving gateway (dedup + cache handled inside)
    print(f"classifying {len(rows)} leads by receiving gateway (MX lookup, cached)...")

    def _prog(i, total, dom, prov, cached):
        if i % 25 == 0 or i == total:
            print(f"  MX {i}/{total}", file=sys.stderr)

    email_col = parsed["email_col"]
    mx_rows = [{"email": r.get(email_col, "")} for r in rows]
    classify.classify_rows(mx_rows, refresh=args.refresh_mx, ttl_days=args.mx_ttl_days,
                           qps=args.mx_qps, progress=_prog)

    items = []
    for raw, m in zip(rows, mx_rows):
        raw["mx_provider"] = m["mx_provider"]
        raw["mx_host"] = m["mx_host"]
        raw["mx_error"] = m["mx_error"]
        items.append({
            "row": raw,
            "email": (raw.get(email_col) or "").strip().lower(),
            "provider": m["mx_provider"],
            "company_key": company_key(raw, args.company_key,
                                       company_field=parsed["company_col"] or "company",
                                       email_field=email_col),
        })

    if args.exclude_unknown:
        before = len(items)
        items = [it for it in items if it["provider"] != "unknown"]
        print(f"--exclude-unknown: dropped {before - len(items)} unknown-gateway leads")
    if not items:
        _err("no leads left to segment")
        return 2

    plan = build_plan(items, base_name=args.name, gap_days=args.gap_days,
                      first_wait=args.first_wait, drip_per_day=args.drip_per_day)

    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd() / f"seg_{csvio.slug(args.name)}"
    manifest = csvio.write_outputs(plan, out_dir=out_dir,
                                   fieldnames=parsed["fieldnames"], items=items)

    _print_report(plan, items, manifest)
    return 0


def _print_report(plan: dict, items: list[dict], manifest: dict) -> None:
    print("\n" + "=" * 64)
    print("SEGMENTATION — gateway distribution & planned segments")
    print("=" * 64)
    print(f"leads classified: {len(items)}\n")
    print("by receiving gateway:")
    for provider in PROVIDER_ORDER:
        d = plan["distribution"].get(provider)
        if not d:
            continue
        tag = "  [PROTECTED]" if provider in PROTECTED else ""
        print(f"  {PROVIDER_LABEL[provider]:11} {d['total']:5}  "
              f"(wave1={d['wave1']}, drip={d['drip']}, companies={d['companies']}){tag}")

    print(f"\nplanned segments: {len(plan['segments'])}")
    for e in plan["segments"]:
        extra = f", drip {e['drip_per_day']}/day" if e["kind"] == "drip" else ""
        role = f", role-inbox {e['role_inbox_count']}" if e["role_inbox_count"] else ""
        print(f"  • {e['name']}")
        print(f"      {e['lead_count']} leads / {e['company_count']} companies · "
              f"{e['step_gap_days']}-day gaps · senders {e['sender_policy']}{extra}{role}")

    cc = Counter(it["company_key"] for it in items)
    big = [(c, n) for c, n in cc.most_common(8) if n > 1]
    if big:
        print("\ntop companies by lead count (Wave 1 sends 1; the rest drip):")
        for c, n in big:
            print(f"  {n:4}  {c}")

    unk = plan["distribution"].get("unknown", {}).get("total", 0)
    if unk:
        print(f"\n{unk} leads had no resolvable gateway (unknown) — see mx_audit.csv")

    print(f"\nmanifest: {manifest['manifest_path']}")
    print(f"audit:    {manifest['mx_audit_csv']}")
    print("\nNo mail has been sent. Each segment CSV is ready to load into your sequencer;")
    print("send Wave 1 first, verify it's bounce-clean, then start the matching Drip.")


# -------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mailgate",
        description="The pre-send control plane for cold-email campaigns: read the "
                    "infrastructure receiving your mail, then turn a flat list into a "
                    "gateway-aware, account-throttled rollout plan. Sends nothing.")
    p.add_argument("--version", action="version", version=f"mailgate {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify", help="look up the receiving gateway for emails/domains")
    c.add_argument("targets", nargs="*", help="emails or bare domains")
    c.add_argument("--file", help="read one email/domain per line from this file")
    c.add_argument("--json", action="store_true", help="emit JSON")
    c.add_argument("--refresh", action="store_true", help="ignore cache, re-resolve")
    c.add_argument("--no-cache", action="store_true", help="don't read or write the cache")
    c.add_argument("--ttl-days", type=int, default=30, help="cache staleness in days (default 30)")
    c.set_defaults(func=cmd_classify)

    s = sub.add_parser("segment", help="classify + wave/drip split a lead CSV")
    s.add_argument("csv", help="lead CSV (needs an email column; all columns pass through)")
    s.add_argument("-n", "--name", required=True, help="base campaign name for the segments")
    s.add_argument("--company-key", choices=("company", "domain", "company_or_domain"),
                   default="company_or_domain", help="per-company throttle key (default company_or_domain)")
    s.add_argument("--gap-days", type=int, default=5, help="recommended days between steps (default 5)")
    s.add_argument("--first-wait", type=int, default=1, help="recommended wait before step 1 (default 1)")
    s.add_argument("--drip-per-day", type=int, default=5, help="recommended new leads/day per Drip (default 5)")
    s.add_argument("--exclude-unknown", action="store_true", help="drop leads with no resolvable gateway")
    s.add_argument("--out-dir", help="output dir (default ./seg_<name>)")
    s.add_argument("--refresh-mx", action="store_true", help="ignore MX cache, re-resolve every domain")
    s.add_argument("--mx-ttl-days", type=int, default=30, help="MX cache staleness in days (default 30)")
    s.add_argument("--mx-qps", type=float, default=5.0, help="MX lookups/sec on cache misses (default 5)")
    s.set_defaults(func=cmd_segment)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
