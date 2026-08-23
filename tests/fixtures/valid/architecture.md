---
title: "Architecture"
aliases:
  - "System Architecture"
tags:
  - "architecture"
  - "system"
summary: "How Lumio is structured internally."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "architecture-doc"
    title: "Architecture decision records"
synthetic: false
---

# Architecture

Lumio is built as a modular monolith with a framework-independent Core SDK.

## Storage

Lumio uses LanceDB for the derived lexical index.
