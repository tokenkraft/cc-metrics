#!/usr/bin/env bash
# Local mirror of .github/workflows/ci.yml — run before publishing.
#
# Exit codes: 0 = every check passed; 3 = passed, but some checks were SKIPPED
# (tool not installed — each skip is printed); 1 = a check failed.
# Runs from the repository root (or any pristine snapshot of it) and writes
# only to a temp dir and Python/ruff caches.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
fail=0; skips=()
step() { printf '\n== %s\n' "$1"; }
need() { command -v "$1" >/dev/null 2>&1 && return 0; skips+=("$1: not installed — $2 SKIPPED"); echo "SKIP: $1 not installed ($2)"; return 1; }
run()  { "$@" || { echo "FAIL: $*" >&2; fail=1; }; }

step "Python: lint + format (ruff $(ruff --version 2>/dev/null | awk '{print $2}'))"
if need ruff "ruff check/format"; then
  run ruff check scripts tests
  run ruff format --check scripts tests
fi

step "Python: unit tests"
run python3 -m unittest discover -s tests

step "Python: unit tests in a tool-less container (CI parity)"
# A CI runner has no claude/paseo/host tooling; a test that silently resolves
# host tools passes every host-side gate and fails only in public CI. Run the
# suite once where no host tool exists, so that class fails here first.
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  # python:3 (not -slim): CI runners ship git, which several tests invoke;
  # parity means matching what CI has, not maximal minimalism.
  run docker run --rm --volume "$PWD:/workspace:ro" --workdir /workspace \
    --env PYTHONDONTWRITEBYTECODE=1 python:3 \
    python3 -m unittest discover -s tests
else
  skips+=("docker unavailable — tool-less container test run SKIPPED"); echo "SKIP: docker unavailable (container test run)"
fi

step "Pricing rules are generated from the manifests"
run python3 scripts/generate_pricing_rules.py --check

step "Shell + JSON"
if need shellcheck "shellcheck scripts/*.sh"; then run shellcheck scripts/*.sh; fi
if need jq "dashboard JSON validation"; then run jq empty grafana/dashboards/*.json; fi

step "Docker Compose config (CI env)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
printf '%s\n' 'ci-only-placeholder-not-a-deployed-secret' > "$tmp/grafana-admin-password"
export HOST_ENV=ci CC_METRICS_RUNTIME_DIR=./runtime GRAFANA_ADMIN_PASSWORD_FILE="$tmp/grafana-admin-password"
if need docker "compose config + collector/promtool validation"; then
  run docker compose --env-file .env.example config --quiet
  if docker info >/dev/null 2>&1; then
    images="$(docker compose --env-file .env.example config --images)"
    collector_image="$(grep '^otel/opentelemetry-collector-contrib:' <<<"$images" | head -1)"
    prometheus_image="$(grep '^prom/prometheus:' <<<"$images" | head -1)"
    step "OpenTelemetry Collector config validate ($collector_image)"
    run docker run --rm --entrypoint /otelcol-contrib --env HOST_ENV=ci \
      --volume "$PWD/otel-collector-config.yaml:/etc/otel-collector-config.yaml:ro" \
      "$collector_image" validate --config=/etc/otel-collector-config.yaml
    step "Prometheus: check config + rules, run rule tests ($prometheus_image)"
    run docker run --rm --entrypoint promtool --volume "$PWD:/workspace:ro" --workdir /workspace \
      "$prometheus_image" check config prometheus.yml
    run docker run --rm --entrypoint promtool --volume "$PWD:/workspace:ro" --workdir /workspace \
      "$prometheus_image" check rules prometheus-rules/ai-unified.yml
    run docker run --rm --entrypoint promtool --volume "$PWD:/workspace:ro" --workdir /workspace \
      "$prometheus_image" test rules tests/prometheus-rules.test.yml
  else
    skips+=("docker daemon not running — collector validate + promtool SKIPPED"); echo "SKIP: docker daemon not running"
  fi
fi

step "Security scanners"
if need gitleaks "secret scan"; then
  # In a checkout: scan committed history exactly like CI (untracked private
  # notes are not the repository). On a history-less snapshot: scan the tree.
  if [ -d .git ]; then run gitleaks git . --no-banner --redact; else run gitleaks dir . --no-banner --redact; fi
fi
if need trivy "vuln/misconfig/secret scan"; then
  run trivy filesystem --scanners vuln,misconfig,secret --severity HIGH,CRITICAL --exit-code 1 .
fi

echo
if [ "$fail" -ne 0 ]; then echo "check: FAIL"; exit 1; fi
if [ "${#skips[@]}" -gt 0 ]; then printf 'check: PASS-WITH-SKIPS\n'; printf '  %s\n' "${skips[@]}"; exit 3; fi
echo "check: PASS"
