# gtm-deliverability

> **Segment a cold-email list by the mail gateway that will actually filter it — then
> throttle per company so you don't get blacklisted.** Sender-agnostic. Nothing sends.

Your cold email isn't judged by the recipient's mailbox. It's judged by the **secure
email gateway** sitting in front of it — Proofpoint, Mimecast, Barracuda. Those
gateways blacklist a sending domain *fast* when you hit ten inboxes at one company on
day one, send everything from a single domain, or move too quickly. Once you're on
their blocklist, every future send to every company behind that gateway lands in
quarantine.

`gtm-deliverability` looks up the receiving gateway for every lead **before** you load
anyone into a sequencer, then fragments the list into deliverability-safe segments:
each gateway isolated, one lead per company in Wave 1, the rest dripped in — round-
robined so consecutive daily sends hit *different* companies.

It doesn't send anything. It produces segmented CSVs + a manifest you feed to whatever
you already use — Instantly, Smartlead, Lemlist, Email Bison, your own SMTP.

```console
$ mailgate classify harvard.edu stanford.edu gmail.com jpmorgan.com microsoft.com
  harvard.edu    proofpoint  mx0a-00171101.pphosted.com  [PROTECTED]
  stanford.edu   proofpoint  mxa-00000d07.gslb.pphosted.com  [PROTECTED]
  gmail.com      google      gmail-smtp-in.l.google.com
  jpmorgan.com   other       cluster14.us.messagelabs.com
  microsoft.com  microsoft   microsoft-com.mail.protection.outlook.com
```

That's a live DNS lookup — no API key, no account, real MX records.

---

## Why this exists

Deliverability is the least-shared, most-painful part of outbound, and almost every
list-building tool ignores the one fact that decides whether you land: **who filters
the mail.** Two lists of the same size behave completely differently if one is 60%
Proofpoint districts and the other is Google-hosted startups. Blast both the same way
and you'll torch your sending domain on the first.

Most "warmup" and "throttle" features operate blind — a flat send rate across the
whole list. This operates on the actual receiving infrastructure:

- **A gateway you can't see is still filtering you.** A school can run Microsoft 365
  behind Proofpoint; the lowest-preference MX is Outlook, but Proofpoint is what
  screens inbound. This tool checks *every* MX host and lets the gateway win.
- **The fastest way to a domain block is bulk-loading one company.** So Wave 1 sends to
  at most one person per company; everyone else drips.
- **Walking a directory top-to-bottom looks like a scrape.** So the drip is round-
  robined across companies.

## Before / after

**Before** — load the whole list into a sequencer, set one global send rate, hope.
Wave-one bounces from the biggest accounts, the gateway flags the domain, and every
later send to that gateway silently quarantines.

**After** — `mailgate segment` splits the list into isolated, per-company-throttled
Wave 1 + Drip segments per gateway. You launch Wave 1, confirm it's bounce-clean, then
start the Drip. The gateway never sees a burst.

## Install

```bash
pip install gtm-deliverability        # once published
# or from source:
git clone https://github.com/kkrlstrm/gtm-deliverability
cd gtm-deliverability && pip install -e .
```

Requires Python 3.9+ and `dnspython`. No API keys, no account, no network calls beyond
public DNS.

## Quickstart

**1. Classify a few domains** (sanity check, no CSV needed):

```bash
mailgate classify districtname.org anotherdistrict.k12.us --json
```

**2. Segment a lead CSV.** Only an `email` column is required; a `company` column
enables the per-company throttle; **every other column rides through untouched** (your
subjects, bodies, personas, whatever):

```bash
mailgate segment leads.csv --name "WI K-12 Q3"
```

```text
================================================================
SEGMENTATION — gateway distribution & planned segments
================================================================
leads classified: 480

by receiving gateway:
  Proofpoint    212  (wave1=140, drip=72, companies=140)  [PROTECTED]
  Mimecast       64  (wave1=51,  drip=13, companies=51)   [PROTECTED]
  Barracuda      33  (wave1=30,  drip=3,  companies=30)   [PROTECTED]
  Microsoft      98  (wave1=88,  drip=10, companies=88)
  Google         51  (wave1=49,  drip=2,  companies=49)
  Unknown        22  (wave1=22,  drip=0,  companies=22)

planned segments: 10
  • WI K-12 Q3 · Proofpoint · Wave 1
      140 leads / 140 companies · 5-day gaps · senders microsoft_only
  • WI K-12 Q3 · Proofpoint · Drip
      72 leads / 68 companies · 5-day gaps · senders microsoft_only, drip 5/day
  ...
```

You get, in `./seg_<name>/`:

| File | What it is |
|---|---|
| `<gateway>_wave1.csv` | One lead per company, per gateway — send these first |
| `<gateway>_drip.csv` | The rest, round-robined across companies — start after Wave 1 is clean |
| `mx_audit.csv` | Every domain → its gateway, MX host, and lead count |
| `manifest.json` | The full plan: segments, cadence, sender policy, counts |

Each segment CSV has your original columns **plus** `mx_provider` / `mx_host` /
`mx_error`. Load them into your sequencer in order: **Wave 1 first, confirm no bounces,
then the matching Drip.** That sequencing *is* the throttle.

## How gateway classification works

For each recipient domain it resolves the MX records over public nameservers
(`1.1.1.1` / `8.8.8.8`, so results don't depend on your local DNS) and matches the MX
hostnames against known gateway fingerprints. Buckets:

| Bucket | Meaning |
|---|---|
| `proofpoint` / `mimecast` / `barracuda` | Protected secure gateways — the hard-to-reach ones |
| `microsoft` / `google` | The big hosted mail platforms |
| `other` | MX resolved but matched no known fingerprint |
| `unknown` | NXDOMAIN / no MX / timeout / malformed — **never guessed into a gateway** |

**A protected gateway wins even when Microsoft or Google is the lowest-preference MX**,
because the gateway is what screens inbound. Classification is deterministic and cached
per domain (`~/.cache/gtm-deliverability/mx_cache.json`), so re-runs on the same list
are near-instant — real lists cluster on a handful of domains. The fingerprint list in
`mailgate/classify.py` is a plain table; add your own in a line.

## The wave / drip model

Within each gateway:

- **Wave 1** = at most **one lead per company**. Which lead is chosen is a stable hash
  of the email, so the plan is reproducible.
- **Drip** = everyone else, **round-robined across companies** so consecutive daily
  adds hit different companies. Recommended `5` new leads/day (`--drip-per-day`).
- **Sender policy** per gateway is carried in the manifest: protected gateways are
  tagged `microsoft_only` (Microsoft→Microsoft is the most trusted path through them),
  everything else `microsoft_preferred`.
- **Role inboxes** (`info@`, `board@`, …) are counted per segment — they behave
  differently and often route straight to quarantine.

None of this sends mail or picks your sending accounts — it's guidance encoded as data,
and the sequencing is enforced by *you* loading Wave 1 before the Drip.

## Works with any sender

The output is plain CSV + JSON. Point it at Instantly, Smartlead, Lemlist, Email Bison,
HubSpot sequences, or your own SMTP loop. `gtm-deliverability` owns the *decision* of
who to send to when; your sequencer owns the send.

## Not in scope (on purpose)

- **It doesn't send, warm up inboxes, or write copy.** It's the pre-send segmentation
  layer, not an ESP.
- **It doesn't verify addresses.** Run your bounce-checker first; feed it clean emails.
- **Compliance is yours.** Cold outreach carries CAN-SPAM and, for government/regulated
  recipients, additional obligations. This tool makes your *sending pattern* safer; it
  does not make a list legal to email. That judgment stays with you.

## CLI reference

```
mailgate classify <emails-or-domains...> [--file F] [--json] [--refresh] [--no-cache] [--ttl-days N]
mailgate segment  <leads.csv> -n "<name>"
                  [--company-key company|domain|company_or_domain]
                  [--gap-days 5] [--first-wait 1] [--drip-per-day 5]
                  [--exclude-unknown] [--out-dir DIR]
                  [--refresh-mx] [--mx-ttl-days 30] [--mx-qps 5]
```

## Testing

The whole suite runs offline — classification takes an injectable resolver, so tests
inject a fake and never touch the network:

```bash
pip install -e ".[dev]"
pytest -q
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
