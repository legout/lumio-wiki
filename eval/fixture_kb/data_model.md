---
title: "Data Model"
id: "entity:data-model"
entity_types:
  - system
aliases: []
tags:
  - "fixture"
  - "retrieval-eval"
summary: "Data Model synthetic fixture page for retrieval evaluation."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "fixture-data_model"
    title: "Data Model"
synthetic: true
claims:
  - id: "claim:data-model-event-schema-1"
    predicate: "relates-to"
    object: "entity:event-schema"
    status: "accepted"
    evidence:
      - section: "Data Model"
  - id: "claim:data-model-entity-schema-2"
    predicate: "relates-to"
    object: "entity:entity-schema"
    status: "accepted"
    evidence:
      - section: "Data Model"
---

# Data Model

The data model for the Astrolabe platform data model defines how schema records flow into the system. See also [the Event Schema](event_schema.md) and the Entity Schema.
