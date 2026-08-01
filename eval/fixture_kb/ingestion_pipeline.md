---
title: "Ingestion Pipeline"
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
relationships:
  - target: "Batch Loader"
    type: "relates-to"
  - target: "Stream Connector"
    type: "relates-to"
synthetic: true
---

# Ingestion Pipeline

The ingestion pipeline for the Astrolabe platform ingestion pipeline defines how batch records flow into the system. See also [the Batch Loader](batch_loader.md) and the Stream Connector.
