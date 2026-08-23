"""Shared Entity/Claim in-memory fixture helpers (ADR-0021, issue #168).

The title-based canonical ``Relationship(target, type)`` frontmatter input is
gone. Canonical edges are accepted entity-to-entity Claims; in-memory records
need no evidence. These helpers keep test page builders terse:

* every page gets ``id=f"entity:{_slug(title)}"``;
* each former ``Relationship(target=T, type=Ty)`` becomes
  ``claim_for(source, T, Ty, n)``.
"""

from __future__ import annotations

import re

from lumio_wiki.records import CLAIM_STATUS_ACCEPTED, Claim


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def entity_id_for(title: str) -> str:
    return f"entity:{_slug(title)}"


def claim_for(source_title: str, target_title: str, predicate: str, n: int = 1) -> Claim:
    """An accepted entity-to-entity Claim (in-memory records carry no evidence)."""
    return Claim(
        id=f"claim:{_slug(source_title)}-{_slug(target_title)}-{n}",
        predicate=predicate,
        object=entity_id_for(target_title),
        status=CLAIM_STATUS_ACCEPTED,
    )
