# Contributing

Issues and pull requests are welcome. Contributions are released under the MIT
[LICENSE](LICENSE).

## Before editing

- Keep secrets, local paths, runtime data, and editor or agent files out of
  commits.
- Cite the source and license for any third-party material you bring in.
- Run the checks below. `tests/test_repository_hygiene.py` scans every shipped
  file for personal paths, email addresses, and credential formats, so keep
  fixtures synthetic.

## Supported scope

Changes target macOS and Ubuntu. Do not add another platform claim without
implementation plus CI/runtime evidence.

Keep environment-specific values in `.env`, based on `.env.example`. Never
hardcode credentials, personal paths, public bind addresses, or deployment
identifiers.

## Generated pricing rules

Edit `pricing/openai-model-pricing.json`, cite official OpenAI source, then:

```console
python3 scripts/generate_pricing_rules.py --write
python3 scripts/generate_pricing_rules.py --check
```

Do not hand-edit generated pricing block.

## Contributor tooling

The checks below need three tools beyond what running the stack requires. Pin
Ruff to the CI version — a different release reformats differently and
`ruff format --check` then fails on code CI accepts.

```console
python3 -m pip install ruff==0.15.16
```

```console
brew install shellcheck jq                  # macOS
sudo apt-get install -y shellcheck jq       # Ubuntu
```

## Required checks

```console
python3 -m unittest discover -s tests -v
python3 scripts/generate_pricing_rules.py --check
ruff check scripts tests
ruff format --check scripts tests
shellcheck scripts/*.sh
jq empty grafana/dashboards/*.json
HOST_ENV=ci CC_METRICS_RUNTIME_DIR=./runtime \
  docker compose --env-file .env.example config --quiet
```

The last command supplies `HOST_ENV` and `CC_METRICS_RUNTIME_DIR` inline because
both ship blank in `.env.example`, and Compose treats blank as missing.

Pull requests changing rules must also pass `promtool check rules` and rule
tests. Image changes require high/critical scans for `linux/amd64` and
`linux/arm64`.

## Documentation

Update README or troubleshooting when behavior changes. State untested cases
explicitly. Provider billing remains authority for cost.
