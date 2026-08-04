# Real-world `lumio-wiki` coding-agent trial

This folder is a staged, realistic test of Lumio's progressive product layers:

1. **Now:** `lumio-wiki[documents]` only — portable Knowledge Base setup, mixed-format ingestion, proposal review, publication, validation, and zero-index retrieval through a coding agent.
2. **Later:** add `lumio-lancedb` and compare enhanced retrieval against the same published Knowledge Base.
3. **Finally:** add the full `lumio` application and exercise the same knowledge through Lumio's browser UI.

The corpus describes **Atlas Heatworks**, a fictional heat-pump installer. It is synthetic, non-sensitive, and deliberately shaped like a small operational corpus: company context, product data, support policy, an installation procedure, and an annual impact report. Facts overlap across documents so the coding agent must reconcile entities, provenance, and Relationships rather than merely copying isolated files.

## Folder layout

```text
.
├── .venv/                 # isolated install; generated and ignored
├── knowledge-base/        # create this through the coding agent; ignored
├── sources/               # raw Knowledge Sources to ingest
│   ├── company-overview.md
│   ├── customer-support-policy.txt
│   ├── product-catalog.html
│   ├── installation-handbook.docx
│   └── 2025-impact-report.pdf
├── evaluation/
│   └── questions.md       # questions to ask after publication; do not ingest
├── tools/
│   ├── generate_binary_sources.py
│   └── validate_sources.py
└── bootstrap.sh
```

## Installation

From this directory:

```bash
./bootstrap.sh
source .venv/bin/activate
lumio-wiki doctor
python tools/validate_sources.py
```

`bootstrap.sh` installs the current repository's `lumio-wiki[documents]` package into this folder's isolated virtual environment. It does **not** install `lumio-lancedb` or the full `lumio` application. The `documents` extra remains part of the `lumio-wiki` distribution and is needed for PDF, DOCX, and HTML conversion.

## Start the coding-agent trial

Start Pi, Codex, Claude Code, or another coding agent **from this directory**, then ask:

> Set up a portable Lumio Knowledge Base in `./knowledge-base` and ingest every file in `./sources`. Use the installed `lumio-wiki` CLI and its coding-agent workflow. Treat me as the Maintainer: keep every mutation proposal-first, show me each proposal's validation and blast radius, and wait for my approval before publishing. Do not install `lumio-lancedb` or the full `lumio` application.

This repository already provides the Lumio project instructions to agents started inside it. For a truly standalone test outside this repository, use the installed CLI's `skill path`, `skill protocol`, or `skill install --agent ...` commands to expose the packaged workflow to the chosen agent.

## Trial boundaries

- Do not ingest `README.md`, `evaluation/`, or `tools/`; only `sources/` contains Knowledge Sources.
- Do not let the agent use the source files directly when answering evaluation questions; answers should come from the published Knowledge Base.
- Keep proposal-first publication enabled.
- Record any confusing command, missing guidance, incorrect conversion, weak proposal, or unsupported claim. These are product findings, not test setup failures to hide.
- The Atlas Heatworks organization, people, products, suppliers, and measurements are fictional.

## Regenerating the binary fixtures

The committed DOCX and PDF are reproducible content fixtures. Regenerate them after editing the generator with:

```bash
uv run --isolated --with python-docx --with reportlab python tools/generate_binary_sources.py
```
