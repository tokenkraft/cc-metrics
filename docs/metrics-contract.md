# Metrics contract

What the pipeline promises, what the cost figures mean, and where the
concurrency guarantees stop.

## Pipeline contract

Client metrics land under one `ai_*` namespace. The collector accepts OTLP,
injects the configured `env`, and drops unapproved resource and datapoint
attributes. Session, thread, producer, and instrumentation-scope identity is
stripped before anything reaches Prometheus, leaving only aggregate Claude
session-start and Codex thread-start counters.

All three clients emit delta temporality: Claude via the exports in setup,
Codex natively as of 0.145.0, Grok Build by default. The collector rejects
unknown metric families and non-delta input, compacts equal safe-label
streams, then converts deltas to cumulative state held in collector memory.

- A collector restart, or a stream idle past `max_stale: 24h`, starts that
  stream's cumulative state over, which Prometheus may read as a counter reset.
- The `max_streams: 10000` cap does something different: once the tracking
  limit is reached, new streams are dropped.
- Inspect restart boundaries when reconciling totals.

The runtime contract is best-effort observability, not financial exactly-once
delivery. Abrupt failure can lose acknowledged points, and retries without
stable event IDs can duplicate them. Provider billing remains the source of
truth.

### Token categories

Codex display categories are disjoint: fresh input is emitted input minus
cache-read and cache-write input; non-reasoning output is emitted output minus
reasoning output; cache read, cache creation, and reasoning output each stay
separate.

Grok Build schema v1 reports `input` inclusive of `cache_read` and `output`
inclusive of `reasoning`, with no cache-write type; the same disjoint
categories are derived from it.

Cost expressions price raw Codex and Grok output, which already includes
reasoning tokens. GPT-5.6 cache-write decomposition is source-backed for Codex
0.145.0 but unproven here against a captured live export fixture; treat it as
version-bound until one exists.

## Cost meaning

Cost panels are operational estimates, never invoices.

- Claude values come from `claude_code.cost.usage`; Anthropic calls them
  approximations and directs users to provider billing.
- Codex values apply rates in `pricing/openai-model-pricing.json` to
  telemetry. They represent standard OpenAI API list-price equivalents.
- Grok values apply rates in `pricing/xai-model-pricing.json` to telemetry, as
  xAI API list-price equivalents at the short-context tier; `grok-4.6` is the
  sole priced model there. xAI publishes no separate reasoning rate, so gross
  output is priced at the output rate.
- Unknown models and unmatched token types are omitted; omitted volume appears
  in the **Unpriced Codex/Grok Tokens** diagnostic.
- Estimates exclude billing modifiers telemetry cannot see: subscription or
  credit charges, Batch/Flex/Priority selection, regional uplift, long-context
  multipliers, and separately billed tools or containers.

Use Claude Console, Amazon Bedrock, Google provider billing, Microsoft provider
billing, OpenAI billing, or xAI billing records as applicable.

## Concurrency correctness boundary

- The pipeline removes producer identity and compacts concurrent producers'
  delta streams safely; compaction adds at most 200 ms before downstream
  processing under normal load.
- The best-effort contract does not promise exactly-once delivery, replay
  deduplication, crash-safe acknowledged batches, strict wall-clock ordering,
  or continuity through collector restart.
- Hook event appends and installer runs are serialized with local lock files
  and atomic renames, validated only on local macOS/Ubuntu file systems.
  Network file systems are outside the supported contract.
- `scripts/ensure-stack.sh` lets one invocation run; a concurrent invocation
  exits successfully.
