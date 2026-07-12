## 1. Partial Manifest Persistence

- [x] 1.1 Add failing tests for atomic failed-gate manifest commit, file-digest verification, strict complete verification rejection, and default query isolation
- [x] 1.2 Implement explicit partial commit and partial manifest verification while preserving the complete-manifest default path

## 2. Isolated Dataset Failures

- [x] 2.1 Add failing tests proving performance profile, attribution log, and normal log errors fail only their own datasets
- [x] 2.2 Preserve failure evidence and return `partial` while retaining all other validated historical backtest files

## 3. Published Entry Verification

- [ ] 3.1 Update the CLI and `self-test` to cover partial archives, retry promotion, and query rejection
- [ ] 3.2 Run focused tests, the full synchronization test suite, and the published entry end-to-end regression
