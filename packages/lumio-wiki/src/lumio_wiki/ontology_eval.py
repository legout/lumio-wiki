"""Model-free evaluation of the entity-claim ontology (issue #173, ADR-0021).

Measures four deterministic behaviours against a versioned gold set —
entity resolution, accepted-edge traversal, canonical/discovery scope
separation, and page-title recall — through the public ``KnowledgeBase``
seams. It never measures answer quality or entailment: there is no
LLM-as-judge, no model provider, and no network. The report discloses the
corpus, mode, warm-up, and fallback policy so every number it prints is
traceable to exactly what ran.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import msgspec

from lumio_wiki.knowledge_base import (
    GRAPH_DIRECTIONS,
    GRAPH_SCOPE_VALUES,
    KnowledgeBase,
)

__all__ = [
    "OntologyEvalReport",
    "OntologyGoldSet",
    "evaluate_ontology",
    "load_ontology_gold_set",
]

#: Static disclosure: how the graph state behind every traversal is obtained.
MODE_DISCLOSURE = "zero-index base layer; in-memory GraphState; no model provider"
#: Static disclosure: warm-up policy. One cold pass, no repeated runs.
WARM_UP_DISCLOSURE = "single cold pass; no warm-up iterations; results are deterministic"
#: Static disclosure: fallback when derived artifacts are missing or stale.
FALLBACK_DISCLOSURE = (
    "a missing, stale, or corrupt graph artifact triggers a deterministic "
    "in-memory-derivation fallback; a missing LanceDB projection falls back "
    "to zero-index"
)
#: Static disclosure: what this evaluation deliberately does NOT measure.
NOT_MEASURED_DISCLOSURE = "answer quality and entailment are not measured"


class _EntityResolutionRow(msgspec.Struct, frozen=True):
    name: str
    entity_id: str = ""
    matched_by: str = ""


class _TitleRecallRow(msgspec.Struct, frozen=True):
    surface: str
    title: str


class _EdgeTraversalRow(msgspec.Struct, frozen=True):
    source: str
    scope: str = "canonical"
    direction: str = "outgoing"
    relationship_type: str | None = None
    expect: list[str] = msgspec.field(default_factory=list)


class _ScopeSeparationRow(msgspec.Struct, frozen=True):
    source: str
    canonical: list[str] = msgspec.field(default_factory=list)
    discovery_only: list[str] = msgspec.field(default_factory=list)


class _OntologyGoldSetYaml(msgspec.Struct, frozen=True):
    name: str = ""
    entity_resolution: list[_EntityResolutionRow] = msgspec.field(default_factory=list)
    title_recall: list[_TitleRecallRow] = msgspec.field(default_factory=list)
    edge_traversal: list[_EdgeTraversalRow] = msgspec.field(default_factory=list)
    scope_separation: list[_ScopeSeparationRow] = msgspec.field(default_factory=list)


#: The public gold-set record: a frozen struct like every other Lumio record.
OntologyGoldSet = _OntologyGoldSetYaml


def load_ontology_gold_set(path: str | Path) -> OntologyGoldSet:
    """Load a YAML ontology gold set (four optional typed sections)."""
    return msgspec.yaml.decode(Path(path).read_text(encoding="utf-8"), type=_OntologyGoldSetYaml)


class AreaAggregate(msgspec.Struct, frozen=True):
    """Measured results for one evaluation area."""

    name: str
    n: int
    correct: int
    rate: float
    failures: list[str] = msgspec.field(default_factory=list)


class OntologyEvalReport(msgspec.Struct, frozen=True):
    """Measured ontology metrics plus the disclosure #173 requires."""

    gold_set: str
    corpus: str
    entity_resolution: AreaAggregate
    title_recall: AreaAggregate
    edge_traversal: AreaAggregate
    scope_separation: AreaAggregate
    mode: str = MODE_DISCLOSURE
    warm_up: str = WARM_UP_DISCLOSURE
    fallback: str = FALLBACK_DISCLOSURE
    not_measured: str = NOT_MEASURED_DISCLOSURE

    @property
    def all_pass(self) -> bool:
        areas = (
            self.entity_resolution,
            self.title_recall,
            self.edge_traversal,
            self.scope_separation,
        )
        return all(a.n == 0 or a.correct == a.n for a in areas)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gold_set": self.gold_set,
            "corpus": self.corpus,
            "entity_resolution": msgspec.to_builtins(self.entity_resolution),
            "title_recall": msgspec.to_builtins(self.title_recall),
            "edge_traversal": msgspec.to_builtins(self.edge_traversal),
            "scope_separation": msgspec.to_builtins(self.scope_separation),
            "all_pass": self.all_pass,
            "disclosure": {
                "mode": self.mode,
                "warm_up": self.warm_up,
                "fallback": self.fallback,
                "not_measured": self.not_measured,
            },
        }

    def to_table(self) -> str:
        lines = [
            f"Ontology evaluation — gold set: {self.gold_set}",
            f"corpus: {self.corpus}  ({MODE_DISCLOSURE})",
            "",
            f"{'area':<22}{'n':>4}{'correct':>9}{'rate':>8}",
        ]
        for area in (
            self.entity_resolution,
            self.title_recall,
            self.edge_traversal,
            self.scope_separation,
        ):
            lines.append(f"{area.name:<22}{area.n:>4}{area.correct:>9}{area.rate:>8.4f}")
        for area in (
            self.entity_resolution,
            self.title_recall,
            self.edge_traversal,
            self.scope_separation,
        ):
            for failure in area.failures:
                lines.append(f"  FAIL {area.name}: {failure}")
        lines += [
            "",
            f"mode:        {self.mode}",
            f"warm-up:     {self.warm_up}",
            f"fallback:    {self.fallback}",
            f"not measured:{' ' + self.not_measured}",
        ]
        return "\n".join(lines)


def _rate(n: int, correct: int) -> float:
    return correct / n if n else 1.0


def evaluate_ontology(kb: KnowledgeBase, gold: OntologyGoldSet) -> OntologyEvalReport:
    """Run the gold set through the public seams; measure; disclose.

    Every metric is an exact deterministic match rate — no sampled or
    model-scored judgement anywhere. Empty sections measure as 1.0 (vacuous
    truth) and are reported with ``n == 0`` so a reader can see they did not
    run.
    """

    # --- entity resolution: exact ID/title/alias/redirect match -------------
    resolution_failures: list[str] = []
    correct = 0
    for row in gold.entity_resolution:
        resolution = kb.resolve_entity(row.name)
        got_id = resolution.entity.id if resolution.entity is not None else ""
        if got_id == row.entity_id and resolution.matched_by == row.matched_by:
            correct += 1
        else:
            resolution_failures.append(
                f"{row.name!r}: expected ({row.entity_id!r}, {row.matched_by!r}), "
                f"got ({got_id!r}, {resolution.matched_by!r})"
            )
    entity_resolution = AreaAggregate(
        name="entity-resolution",
        n=len(gold.entity_resolution),
        correct=correct,
        rate=_rate(len(gold.entity_resolution), correct),
        failures=resolution_failures,
    )

    # --- page-title recall: every entity surface recalls its page title -----
    recall_failures: list[str] = []
    correct = 0
    for row in gold.title_recall:
        resolution = kb.resolve_entity(row.surface)
        title = resolution.entity.title if resolution.entity is not None else ""
        if title == row.title:
            correct += 1
        else:
            recall_failures.append(f"{row.surface!r}: expected {row.title!r}, got {title!r}")
    title_recall = AreaAggregate(
        name="page-title-recall",
        n=len(gold.title_recall),
        correct=correct,
        rate=_rate(len(gold.title_recall), correct),
        failures=recall_failures,
    )

    # --- accepted-edge traversal: exact neighbourhood set match -------------
    traversal_failures: list[str] = []
    correct = 0
    for row in gold.edge_traversal:
        if row.scope not in GRAPH_SCOPE_VALUES:
            traversal_failures.append(f"{row.source!r}: unknown scope {row.scope!r}")
            continue
        if row.direction not in GRAPH_DIRECTIONS:
            traversal_failures.append(f"{row.source!r}: unknown direction {row.direction!r}")
            continue
        got = kb.related_pages(
            row.source,
            scope=row.scope,
            direction=row.direction,
            relationship_type=row.relationship_type,
        )
        if got == sorted(row.expect):
            correct += 1
        else:
            traversal_failures.append(
                f"{row.source!r} ({row.scope}/{row.direction}"
                f"{'/' + row.relationship_type if row.relationship_type else ''}): "
                f"expected {sorted(row.expect)}, got {got}"
            )
    edge_traversal = AreaAggregate(
        name="edge-traversal",
        n=len(gold.edge_traversal),
        correct=correct,
        rate=_rate(len(gold.edge_traversal), correct),
        failures=traversal_failures,
    )

    # --- canonical/discovery separation -------------------------------------
    separation_failures: list[str] = []
    correct = 0
    for row in gold.scope_separation:
        canonical = set(kb.related_pages(row.source, scope="canonical"))
        discovery = set(kb.related_pages(row.source, scope="discovery"))
        expected_canonical = set(row.canonical)
        discovery_only = set(row.discovery_only)
        separated = (
            canonical == expected_canonical
            and not (discovery_only & canonical)
            and discovery_only <= discovery
        )
        if separated:
            correct += 1
        else:
            separation_failures.append(
                f"{row.source!r}: canonical={sorted(canonical)} "
                f"(expected {sorted(expected_canonical)}), "
                f"discovery={sorted(discovery)}, "
                f"discovery-only={sorted(discovery_only)}"
            )
    scope_separation = AreaAggregate(
        name="scope-separation",
        n=len(gold.scope_separation),
        correct=correct,
        rate=_rate(len(gold.scope_separation), correct),
        failures=separation_failures,
    )

    return OntologyEvalReport(
        gold_set=gold.name,
        corpus=str(kb.root),
        entity_resolution=entity_resolution,
        title_recall=title_recall,
        edge_traversal=edge_traversal,
        scope_separation=scope_separation,
    )
