---
title: "Query Engine"
id: "entity:query-engine"
entity_types:
  - system
aliases: []
tags:
  - "fixture"
  - "retrieval-eval"
summary: "Query Engine synthetic fixture page for retrieval evaluation."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "fixture-query_engine"
    title: "Query Engine"
synthetic: true
claims:
  - id: "claim:query-engine-sql-frontend-1"
    predicate: "relates-to"
    object: "entity:sql-frontend"
    status: "accepted"
    evidence:
      - section: "Query Engine"
  - id: "claim:query-engine-vector-index-2"
    predicate: "relates-to"
    object: "entity:vector-index"
    status: "accepted"
    evidence:
      - section: "Query Engine"
---

# Query Engine

The query engine for the Astrolabe platform query engine defines how frontend records flow into the system. See also [the SQL Frontend](sql_frontend.md) and the Vector Index.
