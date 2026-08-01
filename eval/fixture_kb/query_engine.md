---
title: "Query Engine"
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
relationships:
  - target: "SQL Frontend"
    type: "relates-to"
  - target: "Vector Index"
    type: "relates-to"
synthetic: true
---

# Query Engine

The query engine for the Astrolabe platform query engine defines how frontend records flow into the system. See also [the SQL Frontend](sql_frontend.md) and the Vector Index.
