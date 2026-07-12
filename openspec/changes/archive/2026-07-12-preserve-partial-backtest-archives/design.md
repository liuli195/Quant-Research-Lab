## Context

Historical backtest synchronization builds all datasets in a temporary directory and calls `commit_manifest` only when `gate.status=pass`. A single performance profile, attribution log, or normal log failure therefore deletes the temporary batch even after code, parameters, Research data, and other page evidence have been validated. The existing specification already requires those validated files to be preserved, so this change corrects implementation drift.

## Goals / Non-Goals

**Goals:**

- Atomically preserve validated files, failed dataset state, and reviewable evidence in a partial archive.
- Keep `gate.status=pass` as the only condition for a complete archive and default querying.
- Let later synchronization read a partial manifest and replace it with a complete manifest after all datasets pass.

**Non-Goals:**

- Do not relax any dataset completeness rule.
- Do not expose partial archives to default query or export paths.
- Do not add dataset-level incremental download or migrate existing missing backtests in this change.

## Decisions

1. **Keep `manifest.json` as the single authoritative object entry.** A failed gate explicitly identifies a partial archive. A second `partial-manifest.json` would duplicate identity and file-reference state.
2. **Require an explicit partial commit path.** The default `commit_manifest` path still rejects failed gates. Synchronization may explicitly commit a partial manifest only after it has a stable collection fence and complete per-dataset states. Partial commit validates manifest structure and every referenced file digest without treating failed datasets as complete.
3. **Convert isolated collection failures into dataset state.** Performance profile, attribution log, and normal log failures record `failed`, error type, and error message. Raw failing source bytes are compressed and referenced when available. Other datasets continue through their existing strict builders.
4. **Keep reads fail-closed.** `verify` can validate partial manifest structure and referenced file digests and returns `partial`. `query` and `export-csv` continue to reject a failed gate.
5. **Reuse the current whole-object retry.** A successful retry replaces the partial manifest with a complete manifest. Dataset-level cursors and caching remain out of scope.
6. **Continue the tweak preset after the file-count tripwire.** The user confirmed the change remains one focused archive-behavior correction and does not need a full workflow upgrade.

## Risks / Trade-offs

- **A partial manifest could be mistaken for complete.** → CLI status is `partial`, while default query still requires a passing gate.
- **Failing source data may not be parseable.** → Verify raw byte digests and failure evidence only; never mark that dataset complete.
- **Retries redownload successful datasets.** → Accept the cost to keep this fix small and stop data loss first.
- **A retry may encounter immutable file conflicts.** → Preserve existing digest conflict protection and reuse only identical files.

## Migration Plan

No bulk migration is required. Re-synchronizing a missing target after deployment creates a partial manifest. Existing complete archives remain unchanged. Rolling back the code leaves failed-gate manifests unavailable to default query paths.

## Implementation Divergence

The capability specification retains the existing goal of fetching only affected datasets on retry. This focused tweak does not add dataset-level retry cursors: a retry recollects the whole backtest, while commit-time SHA256 checks reuse identical archived files. The extra download cost is accepted here to keep the data-loss fix isolated; dataset-level incremental retry remains a separate follow-up.

## Open Questions

None.
