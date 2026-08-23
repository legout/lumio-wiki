---
title: "Access Control"
id: "entity:access-control"
entity_types:
  - system
aliases: []
tags:
  - "fixture"
  - "retrieval-eval"
summary: "Access Control synthetic fixture page for retrieval evaluation."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "fixture-access_control"
    title: "Access Control"
synthetic: true
claims:
  - id: "claim:access-control-role-model-1"
    predicate: "relates-to"
    object: "entity:role-model"
    status: "accepted"
    evidence:
      - section: "Access Control"
  - id: "claim:access-control-audit-trail-2"
    predicate: "relates-to"
    object: "entity:audit-trail"
    status: "accepted"
    evidence:
      - section: "Access Control"
---

# Access Control

The access control for the Astrolabe platform access control defines how role records flow into the system. See also [the Role Model](role_model.md) and the Audit Trail.
