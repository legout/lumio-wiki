---
title: "Storage Architecture"
id: "entity:storage-architecture"
entity_types:
  - system
aliases: []
tags:
  - "fixture"
  - "retrieval-eval"
summary: "Storage Architecture synthetic fixture page for retrieval evaluation."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "fixture-storage_architecture"
    title: "Storage Architecture"
synthetic: true
claims:
  - id: "claim:storage-architecture-cold-tier-1"
    predicate: "relates-to"
    object: "entity:cold-tier"
    status: "accepted"
    evidence:
      - section: "Storage Architecture"
  - id: "claim:storage-architecture-hot-cache-2"
    predicate: "relates-to"
    object: "entity:hot-cache"
    status: "accepted"
    evidence:
      - section: "Storage Architecture"
---

# Storage Architecture

The storage architecture for the Astrolabe platform storage architecture defines how tier records flow into the system. See also [the Cold Tier](cold_tier.md) and the Hot Cache.
