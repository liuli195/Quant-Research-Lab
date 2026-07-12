## MODIFIED Requirements

### Requirement: Every expected dataset has an independent completeness state
Each archived object's `manifest.json` MUST list every expected dataset and record its source, row or byte count, time range, pagination evidence, file SHA256, and one of `complete`, `capped_free`, `missing_at_source`, `unsupported_api_version`, or `failed`. An object may pass the completeness gate only when every required core dataset is `complete` and every other expected dataset has an explicitly accepted state. An object that has a stable collection fence but does not pass the gate MUST atomically preserve validated files, failed dataset state, and failure evidence with `gate.status=fail`. A partial archive MUST NOT be available through default query or export paths and MUST NOT be reported as complete.

#### Scenario: One dataset is incomplete while others are complete
- **WHEN** results and balances are complete but position pagination is incomplete
- **THEN** the system atomically preserves validated data and failure evidence, marks positions as `failed`, saves the object as a partial archive with `gate.status=fail`, and prevents the object from passing the completeness gate

#### Scenario: One non-core dataset fails
- **WHEN** a performance profile, normal log, or another non-core dataset fails after code, parameters, a stable collection fence, and all other datasets have been validated
- **THEN** the system preserves all other validated files, records the failed dataset and reason, returns `partial`, and keeps the object unavailable to default query and export paths

#### Scenario: Remote inventory changes during synchronization
- **WHEN** the remote object inventory reread at the end of synchronization differs from the inventory read at the start
- **THEN** the system retries only changed content and does not atomically commit a batch that failed collection-fence validation

#### Scenario: Synchronizing the same object again
- **WHEN** the remote digest and cursors for the same target have not changed
- **THEN** the system verifies existing file digests and skips duplicate downloads; when content is missing or changed, it fetches only the affected datasets

#### Scenario: A retry completes a partial archive
- **WHEN** the same target already has a partial manifest with `gate.status=fail` and a later synchronization obtains every required dataset and valid evidence
- **THEN** the system verifies and reuses identical existing files and atomically replaces the partial manifest with a complete passing manifest

#### Scenario: Verifying or querying a partial archive
- **WHEN** a caller verifies or queries an object with `gate.status=fail`
- **THEN** verification validates manifest structure and referenced file digests and explicitly returns `partial`, while default query and export paths reject the object
