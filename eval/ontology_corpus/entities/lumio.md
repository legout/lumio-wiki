---
id: "entity:lumio"
title: "Lumio"
entity_types:
  - software-system
aliases:
  - "Lumio Knowledge Platform"
tags:
  - "ontology-parity"
summary: "Deployable chat for trusted knowledge and data."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "lumio-product"
    title: "Lumio product overview"
claims:
  - id: "claim:lumio-uses-lancedb"
    predicate: "uses"
    object: "entity:lancedb"
    status: "accepted"
    evidence:
      - section: "Retrieval"
  - id: "claim:lumio-uses-obstore-v1"
    predicate: "uses"
    object: "entity:obstore"
    status: "superseded"
    evidence:
      - section: "Storage"
  - id: "claim:lumio-uses-obstore-v2"
    predicate: "uses"
    object: "entity:obstore"
    status: "accepted"
    evidence:
      - section: "Storage"
  - id: "claim:lumio-replaces-sage-wiki"
    predicate: "replaces"
    object: "entity:sage-wiki"
    status: "disputed"
    evidence:
      - section: "Overview"
  - id: "claim:lumio-first-released"
    predicate: "first-released"
    value: 2025
    value_type: number
    status: "accepted"
    evidence:
      - section: "Overview"
---

# Lumio

## Overview

Lumio is a deployable chat platform for trusted knowledge and data. It first
shipped in 2025. See also [Sage Wiki](sage-wiki.md) and the
[[Knowledge Graph]].

## Retrieval

Lumio uses [LanceDB](lancedb.md) for optional enhanced retrieval.

## Storage

Lumio publishes immutable versions to object storage through obstore.
