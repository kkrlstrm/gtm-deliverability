# Security & data handling

`gtm-deliverability` is a local CLI. It does not phone home, require an account, or
transmit your lead list anywhere.

- **Network:** the only outbound traffic is DNS `MX` queries to public resolvers
  (`1.1.1.1`, `8.8.8.8`) for the recipient domains in your list. No email addresses,
  names, or CSV contents leave your machine — only bare domains are resolved.
- **On disk:** a domain→gateway cache at `~/.cache/gtm-deliverability/mx_cache.json`
  (domains and their MX records only — no lead PII). Segment CSVs and the manifest are
  written wherever you point `--out-dir` (default `./seg_<name>/`); they contain your
  lead data, so treat that directory like any list export.
- **No secrets:** the tool takes no API keys and stores none.

## Reporting a vulnerability

Open a private security advisory on the GitHub repository, or email the maintainer.
Please don't file public issues for security reports.
