# RECON Backlog — Technical Debt

## Qdrant Migration Follow-ups (2026-04-28)

From the Qdrant source-of-truth migration pre-commit review. All nice-to-haves, zero production impact.

1. **domain_assigner.py — list-type domain handling**
   `_count_domains_from_qdrant` counts every element in a list-type `domain` payload. The embedder's `_validate_classification` normalizes lists to `payload['domain'] = valid[0]` before upsert, so multi-element lists never exist in production Qdrant data. For spec consistency, could match the embedder's first-only normalization. Zero impact since the embedder guarantees bare strings.

2. **recon.py --tiebreaker-pass — Qdrant client threading**
   The `--tiebreaker-pass` CLI branch calls `run_tiebreaker_pass(db, config)` without creating and passing a `QdrantClient`. The function handles this via lazy construction in `_get_qdrant_client`, which creates one client for the entire batch. Could thread a client from the CLI entry point for consistency with `--backfill`. Functionally fine as-is.

3. **_get_qdrant_client — debug log caller identification**
   The debug log `"Creating new QdrantClient (caller did not pass one)"` doesn't identify which function triggered the lazy construction. Could include caller info (e.g., `inspect.stack()[1].function`) for easier debug session triage. Low priority since it only fires for lazy construction paths.
