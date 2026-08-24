---
id: "entity:knowledge-graph"
title: "Knowledge Graph"
entity_types:
  - concept
tags:
  - "ontology-parity"
summary: "The canonical graph of accepted, evidence-bearing Claims."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "adr-0021"
    title: "ADR-0021: Entity-Claim Ontology"
claims:
  - id: "claim:kg-described"
    predicate: "described-as"
    value: "accepted Claims plus Extracted References for discovery"
    value_type: string
    status: "accepted"
    evidence:
      - section: "Definition"
---

# Knowledge Graph

## Definition

The Knowledge Graph is the canonical graph of accepted, evidence-bearing
Claims. The Discovery Graph adds deterministic Extracted References; see
[[Lumio]] for the platform that compiles it.
