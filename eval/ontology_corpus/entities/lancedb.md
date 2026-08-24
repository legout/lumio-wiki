---
id: "entity:lancedb"
title: "LanceDB"
entity_types:
  - library
aliases:
  - "Lance Columnar Store"
tags:
  - "ontology-parity"
summary: "Embedded vector database used for enhanced retrieval."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "lancedb-docs"
    title: "LanceDB documentation"
claims:
  - id: "claim:lancedb-used-by-lumio"
    predicate: "used-by"
    object: "entity:lumio"
    status: "accepted"
    evidence:
      - section: "Adoption"
  - id: "claim:lancedb-described"
    predicate: "described-as"
    value: "an embedded columnar vector store"
    value_type: string
    status: "accepted"
    evidence:
      - section: "Overview"
---

# LanceDB

## Overview

LanceDB is an embedded columnar vector store.

## Adoption

LanceDB is used by [Lumio](lumio.md) for enhanced retrieval.
