## Why

Historical backtest synchronization currently deletes the entire staged batch when any one dataset fails, so already validated code, Research data, and page evidence are lost. This contradicts the existing requirement to preserve validated data, mark the failing dataset as `failed`, and keep the object behind a failed completeness gate.

## What Changes

- Atomically preserve validated files, failed dataset state, and failure evidence when a historical backtest is only partially complete, and return `partial`.
- Keep the completeness gate unchanged: a partial archive retains `gate.status=fail`, is never reported as complete, and remains unavailable to default query and export paths.
- Allow a later synchronization of the same target to read the partial manifest and replace it with a complete manifest after all datasets pass.
- Preserve reviewable failure reasons for performance profiles, attribution logs, and normal logs without misclassifying them as `complete`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `joinquant-archive-sync`: Clarify persistence, verification, query isolation, and repair behavior for partial archives.

## Impact

- Affects historical backtest synchronization, manifest commit and verification logic, related unit tests, and the in-memory end-to-end regression.
- Does not change cloud execution boundaries, authentication, completeness pass criteria, or the format of existing complete archives.
- Excludes the existing downloaded backtest data and manual review report in the dirty worktree.
