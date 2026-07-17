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
classify — resolve a domain's MX records and classify the receiving mail gateway.

The point: cold email is often filtered less by the mailbox provider than by the
**secure email gateway** sitting in front of it — Proofpoint, Mimecast, Barracuda.
Different receiving environments filter differently, so a flat list shouldn't be
launched as one homogeneous campaign. Before you load anyone into a sequencer, look up
each recipient domain's MX records and bucket it by the gateway that will actually
screen the inbound mail — so each cohort can be launched, monitored, and sender-
assigned independently. This module only classifies; the throttling lives in
`segment.py`.

Classification is deterministic and offline-testable: `classify_domain` takes an
injectable resolver, so tests pass a fake and never touch the network. A per-domain
JSON cache makes re-runs on the same list near-instant (real lists cluster on a
handful of domains).

Buckets (a "protected" gateway wins even when Microsoft/Google is the lowest-pref MX,
because the gateway is what filters inbound):

    proofpoint | mimecast | barracuda      (the hard-to-reach secure gateways)
    microsoft  | google                    (the big hosted mail platforms)
    other                                   (MX resolved, matched nothing above)
    unknown                                 (NXDOMAIN / no MX / timeout / bad domain)

Standalone: depends only on `dnspython`. No other imports.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from pathlib import Path

import dns.exception
import dns.resolver

CACHE_PATH = Path.home() / ".cache" / "gtm-deliverability" / "mx_cache.json"

# Provider buckets the segmenter reasons about.
PROTECTED = ("proofpoint", "mimecast", "barracuda")
PROVIDERS = PROTECTED + ("microsoft", "google", "other", "unknown")

# Ordered substring patterns matched against lowercased MX hostnames.
# Protected gateways are checked first and across ALL MX hosts (see _classify_hosts),
# so a domain fronted by a gateway but hosted on M365/Google still buckets to the gateway.
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("proofpoint", ("pphosted.com", "ppe-hosted.com", ".pphosted", ".ppe-hosted")),
    ("mimecast", ("mimecast.com", ".mimecast")),
    ("barracuda", ("barracudanetworks.com", ".ess.barracuda", "cudasvc.com", "barracuda.com")),
    ("microsoft", (".mail.protection.outlook.com", ".olc.protection.outlook.com", "outlook.com")),
    ("google", ("aspmx.l.google.com", ".google.com", "googlemail.com", "aspmx.l.google", ".googlemail")),
]


def domain_of(email: str) -> str:
    """Lowercased domain part of an email; '' if it doesn't look like an address."""
    s = (email or "").strip().lower()
    if "@" not in s:
        return ""
    dom = s.rsplit("@", 1)[1].strip().strip(".")
    # strip trailing display junk / mailto leftovers defensively
    dom = re.sub(r"[^a-z0-9.\-]", "", dom)
    return dom


def _match_provider(host: str) -> str | None:
    h = host.lower().rstrip(".")
    for provider, needles in _PATTERNS:
        if any(n in h for n in needles):
            return provider
    return None


def _classify_hosts(hosts: list[str]) -> str:
    """Bucket a list of MX hostnames. Protected gateways win over microsoft/google."""
    matches = [m for m in (_match_provider(h) for h in hosts) if m]
    if not matches:
        return "other" if hosts else "unknown"
    for p in PROTECTED:  # proofpoint > mimecast > barracuda, deterministic
        if p in matches:
            return p
    if "microsoft" in matches:
        return "microsoft"
    if "google" in matches:
        return "google"
    return "other"


def make_resolver() -> dns.resolver.Resolver:
    """Resolver pinned to public nameservers so results don't depend on local DNS."""
    r = dns.resolver.Resolver(configure=False)
    r.nameservers = ["1.1.1.1", "8.8.8.8"]
    r.timeout = 5
    r.lifetime = 10
    return r


def _today() -> str:
    return date.today().isoformat()


def _resolve_mx(domain: str, resolver) -> tuple[list[tuple[int, str]], str]:
    """Return ([(pref, host), ...] sorted by pref, error_string). One retry on timeout."""
    last_err = ""
    for attempt in range(2):
        try:
            ans = resolver.resolve(domain, "MX")
            recs = sorted(
                ((int(r.preference), str(r.exchange).rstrip(".").lower()) for r in ans),
                key=lambda t: t[0],
            )
            if not recs:
                return [], "no_mx"
            return recs, ""
        except dns.resolver.NXDOMAIN:
            return [], "NXDOMAIN"
        except dns.resolver.NoAnswer:
            return [], "no_mx"
        except dns.resolver.NoNameservers:
            last_err = "no_nameservers"
        except dns.exception.Timeout:
            last_err = "timeout"
        except Exception as e:  # malformed domain, etc.
            return [], f"error:{type(e).__name__}"
        if attempt == 0 and last_err == "timeout":
            continue
        break
    return [], last_err or "error"


def classify_domain(domain: str, cache: dict, resolver=None, *,
                    refresh: bool = False, ttl_days: int = 30) -> dict:
    """
    Classify one domain. Uses/updates `cache` (mutated in place). Pass a resolver to
    inject (tests use a fake); None lazily builds the public-nameserver resolver.

    Returns: {domain, provider, mx_host, pref, all_mx, error, resolved_at}
    """
    domain = (domain or "").strip().lower().strip(".")
    if not domain or "." not in domain:
        return {"domain": domain, "provider": "unknown", "mx_host": "", "pref": None,
                "all_mx": [], "error": "bad_domain", "resolved_at": _today()}

    cached = cache.get(domain)
    if cached and not refresh and not _is_stale(cached, ttl_days):
        return cached

    if resolver is None:
        resolver = make_resolver()
    recs, err = _resolve_mx(domain, resolver)
    hosts = [h for _, h in recs]
    provider = _classify_hosts(hosts) if not err else "unknown"
    # the representative host: the matched-gateway host if a gateway won, else lowest-pref
    mx_host = ""
    if hosts:
        gw_host = next((h for h in hosts if _match_provider(h) == provider), None)
        mx_host = gw_host or hosts[0]
    rec = {
        "domain": domain,
        "provider": provider,
        "mx_host": mx_host,
        "pref": recs[0][0] if recs else None,
        "all_mx": [f"{p}:{h}" for p, h in recs],
        "error": err,
        "resolved_at": _today(),
    }
    cache[domain] = rec
    return rec


def _is_stale(rec: dict, ttl_days: int) -> bool:
    try:
        ra = datetime.fromisoformat(rec.get("resolved_at", "")).date()
    except Exception:
        return True
    return (date.today() - ra).days > ttl_days


def load_cache(path: Path = CACHE_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def save_cache(cache: dict, path: Path = CACHE_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2, sort_keys=True))


def classify_rows(rows: list[dict], *, email_key: str = "email", refresh: bool = False,
                  ttl_days: int = 30, qps: float = 5.0, resolver=None,
                  cache_path: Path = CACHE_PATH, progress=None) -> list[dict]:
    """
    Classify every row by its email domain. Adds keys: mx_provider, mx_host, mx_error.
    Dedupes to unique domains (one lookup each); throttles only on cache MISSES so the
    public resolvers aren't hammered. Persists the cache at the end.
    """
    cache = load_cache(cache_path)
    if resolver is None:
        resolver = make_resolver()
    sleep = 1.0 / qps if qps and qps > 0 else 0.0

    # resolve unique domains first
    domains = []
    seen = set()
    for r in rows:
        d = domain_of(r.get(email_key, ""))
        if d and d not in seen:
            seen.add(d)
            domains.append(d)

    results: dict[str, dict] = {}
    for i, d in enumerate(domains):
        was_cached = (d in cache) and not refresh and not _is_stale(cache[d], ttl_days)
        rec = classify_domain(d, cache, resolver, refresh=refresh, ttl_days=ttl_days)
        results[d] = rec
        if progress:
            progress(i + 1, len(domains), d, rec["provider"], was_cached)
        if sleep and not was_cached:
            time.sleep(sleep)

    save_cache(cache, cache_path)

    for r in rows:
        d = domain_of(r.get(email_key, ""))
        rec = results.get(d) or {"provider": "unknown", "mx_host": "", "error": "bad_domain"}
        r["mx_provider"] = rec["provider"]
        r["mx_host"] = rec.get("mx_host", "")
        r["mx_error"] = rec.get("error", "")
    return rows
