"""Shared fixtures for the retrieval-evaluation gate (issue #138).

The gate runs against the committed synthetic fixture KB + versioned gold set
that live alongside this directory. They are deterministic: no provider, no
network, no LanceDB at the base layer (LanceDB stages opt in when installed).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lumio_wiki import retrieval_eval as ev
from lumio_wiki.knowledge_base import load_knowledge_base

EVAL_DIR = Path(__file__).parent
FIXTURE_KB = EVAL_DIR / "fixture_kb"
GOLD_SET = EVAL_DIR / "gold_set.yaml"


@pytest.fixture(scope="session")
def fixture_kb():
    kb, report = load_knowledge_base(FIXTURE_KB)
    assert report.is_valid, "fixture KB must be valid"
    return kb


@pytest.fixture(scope="session")
def gold_set() -> ev.GoldSet:
    return ev.load_gold_set(GOLD_SET)
