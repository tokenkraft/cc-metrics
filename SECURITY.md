# Security policy

## Support boundary

Security support covers current repository state on macOS and Ubuntu; no other
platform claim exists.

Stack is local-only by default:

- Grafana has password authentication.
- Prometheus and collector have no application authentication.
- OTLP and web ports bind to loopback through `.env.example`.
- Collector drops metric attributes not on explicit allowlist.
- Privacy transform removes producer and instrumentation-scope identity before
  serialized batch compaction. Supported delta metrics are summed by safe
  output labels before cumulative conversion.
- Client logs, traces, prompts, and tool content are not enabled by documented
  setup.

Changing bind address or telemetry signals creates different threat model and
is unsupported without separate authentication, TLS, firewall, privacy, and
retention controls.

## Report vulnerability

Report privately through this repository's GitHub **Security → Report a
vulnerability** tab. Please do not open a public issue for a suspected
vulnerability.

Do not put exploit details, credentials, raw telemetry, personal paths, or
identity-bearing labels in a public issue.

## Secret exposure

If credential enters Git history:

1. revoke or rotate it at provider;
2. stop using affected credential;
3. remove it from the working tree, and from history using a documented
   history-rewrite procedure such as `git filter-repo`;
4. run secret scan across full history;
5. document incident without reproducing secret.

Deleting one file or commit does not revoke credential.

## Deployment security

Before use:

- generate unique Grafana password;
- remember password secret seeds empty Grafana database only;
- keep `.env`, `.secrets/`, and runtime data untracked;
- keep host binding at `127.0.0.1`;
- scan container images for the host architecture before deploying, for
  example `trivy image` against the pinned references;
- review Codex hook command before trusting it;
- inspect retained Prometheus labels before sharing data.

Provider API keys belong only to authenticated clients, never this stack.

## Grafana administrator password

`GRAFANA_ADMIN_PASSWORD_FILE` supplies initialization secret. Once Grafana data
volume contains database, changing file or restarting container does not rotate
stored administrator password.

Rotate through authenticated Grafana UI. Store same new value in local secret
file for future initialization. For lost password, follow official Grafana CLI
reset guidance on trusted host; CLI password argument can be visible to process
inspection. Do not delete data volume merely to rotate credential.
