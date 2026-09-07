# myKG experiment report — issue #146

> **Template.** Fill this in **after** the myKG run described in
> [`README.md`](README.md). Until the four missing maintainer inputs are provided
> and the experiment is run, every measurement below is `—` (not yet collected).
> Do not invent values.

## Run metadata

| field | value |
| --- | --- |
| Issue | [#146](https://github.com/legout/lumio/issues/146) |
| Run date | `—` |
| Corpus (5–10 non-sensitive docs) | `—` |
| myKG version | `—` |
| myKG model / provider config (schema-review on) | `—` |
| Cost + elapsed-time budget | `—` |
| Experiment-artifact storage + retention (private) | `—` |
| Baseline distiller | `—` |

## 1. Conversion summary

Paste the `summary` block from `conversion-report.json`:

```text
pages               : —
candidates          : —
skipped_nodes       : —
title_collisions    : —
warnings            : —
```

Notable collisions / warnings: `—`

## 2. Measurements (issue #146)

| metric | baseline (per-source Distiller) | myKG-assisted |
| --- | --- | --- |
| Duplicate entities | — | — |
| Canonical Page Title / alias collisions | — | — |
| Unsupported or hallucinated attributes | — | — |
| Relationship acceptance rate | — | — |
| Relationship modification rate | — | — |
| Relationship rejection rate | — | — |
| Source-passage correctness | — | — |
| Pages accepted vs discarded | — | — |
| Maintainer review time | — | — |
| Extraction latency | — | — |
| Model cost | — | — |

Notes on relationship candidates (relation types that needed semantic correction,
exact vs collapsed mappings, dangling endpoints): `—`

## 3. Success criteria (issue #146)

- [ ] Every accepted page has valid Lumio provenance.
- [ ] No myKG edge becomes canonical without Maintainer review.
- [ ] At least 85% of accepted relationship candidates need no semantic correction.
- [ ] Entity duplication is lower than the current per-source Distiller baseline.
- [ ] The candidate Knowledge Base passes normal validation and lint.
- [ ] Maintainer review effort is lower than manually reconciling the baseline proposals.
- [ ] No raw source material or private extraction metadata leaks into Reader-visible artifacts or exports.

## 4. Stop conditions (issue #146)

Did any stop condition trigger? `—`

- Relationship precision poor? `—`
- One-entity-per-page floods the Knowledge Base with low-value pages? `—`
- Identity collisions require extensive manual cleanup? `—`
- Review burden exceeds direct Lumio distillation? `—`
- Privacy / export isolation cannot be demonstrated? `—`

## 5. Privacy / export audit

- [ ] Raw source paths absent from every Compiled Page frontmatter and body.
- [ ] Source text / chunks absent from every Compiled Page and public export.
- [ ] `relationship-candidates.jsonl` (confidence, method, source files) stays out of the published KB and exports.
- [ ] `conversion-report.json` (`private_provenance`, raw source map) stays out of the published KB and exports.
- [ ] Opaque source ids cannot be reversed to raw files without the private registry.

Evidence / commands used to verify (e.g. `grep` over the candidate tree and an
OKF export): `—`

## 6. Decision

`adopt` / `defer` / `reject`: `—`

Rationale: `—`

## 7. If `adopt` — follow-up

- [ ] Proposed engineering documents / issues: `—`
- [ ] Is an ADR required for process / package ownership? `—` (draft title: `—`)
- [ ] Do private proposal diagnostics need a focused interface spec? `—`
- [ ] Confirm no general extractor-provider interface is introduced yet (one adapter remains a hypothetical seam).
