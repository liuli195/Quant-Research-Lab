# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root if it exists.
- **`docs/comet/specs/`** — read specifications that touch the area you're about to work in. These specifications are the authoritative source for implementation and review.

If any of these files don't exist, **proceed silently**. Don't flag their absence or suggest creating them upfront. The `/domain-modeling` skill creates `CONTEXT.md` lazily when terms actually get resolved.

## File structure

This is a single-context repo:

```
/
├── CONTEXT.md
├── docs/comet/specs/
└── src/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, or a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, either reconsider language the project doesn't use or note a real gap for `/domain-modeling`.

## Follow authoritative specifications

Treat relevant files in `docs/comet/specs/` as authoritative. If an output or implementation conflicts with a specification, surface the conflict explicitly and follow the specification rather than silently overriding it.
