# AGENTS.md

## Project Context

This repository contains the new `llm-wiki-agent` project: a deployable browser agent platform for compiled LLM wikis.

Before implementation work, read:

- `docs/superpowers/specs/2026-07-03-deployable-knowledge-agent-design.md`

The intended architecture is an SDK-centered modular monolith: one deployable app for the MVP, with a reusable Knowledge Base Core SDK and thin clients for web, external chat UIs, future CLI, and local coding agents.
