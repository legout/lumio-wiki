---
title: "Ingestion Pipeline"
id: "entity:ingestion-pipeline"
entity_types:
  - system
aliases: []
tags:
  - "fixture"
  - "retrieval-eval"
summary: "Ingestion Pipeline synthetic fixture page for retrieval evaluation."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "fixture-ingestion_pipeline"
    title: "Ingestion Pipeline"
synthetic: true
claims:
  - id: "claim:ingestion-pipeline-batch-loader-1"
    predicate: "relates-to"
    object: "entity:batch-loader"
    status: "accepted"
    evidence:
      - section: "Ingestion Pipeline"
  - id: "claim:ingestion-pipeline-stream-connector-2"
    predicate: "relates-to"
    object: "entity:stream-connector"
    status: "accepted"
    evidence:
      - section: "Ingestion Pipeline"
---

# Ingestion Pipeline

The ingestion pipeline for the Astrolabe platform ingestion pipeline defines how batch records flow into the system. See also [the Batch Loader](batch_loader.md) and the Stream Connector.
