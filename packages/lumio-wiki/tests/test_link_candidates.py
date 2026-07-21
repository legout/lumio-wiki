"""Tests for the deterministic link-candidate finder (issue #90, ADR-0011).

The finder scans Compiled Page bodies for UNLINKED mentions of known Canonical
Page Titles or Aliases and emits non-canonical LinkCandidate proposals. It
complements the Extracted Reference resolver (issue #107): a mention already
inside a Markdown link or wikilink is handled by that resolver and is never
re-proposed here. A candidate never enters the Discovery Graph until a
Maintainer approves and publishes the Markdown proposal (#91).

All tests are zero-index, deterministic, and model-free.
"""

from __future__ import annotations

from lumio_wiki.knowledge_base import extract_references
from lumio_wiki.link_candidates import find_link_candidates
from lumio_wiki.records import CompiledPage, LinkCandidate, Source


def _page(
    title: str,
    *,
    body: str = "",
    aliases: list[str] | None = None,
    path: str | None = None,
    body_start_line: int = 1,
) -> CompiledPage:
    return CompiledPage(
        path=path or f"{title.lower().replace(' ', '-')}.md",
        title=title,
        aliases=aliases or [],
        tags=["test"],
        summary=f"{title} summary",
        lifecycle="approved",
        visibility="public",
        sources=[Source(id=f"src-{title.lower()}", title=title)],
        body=body,
        body_start_line=body_start_line,
    )


# ---------------------------------------------------------------------------
# 1. A plain mention of a known title emits a candidate with full provenance.
# ---------------------------------------------------------------------------


def test_plain_title_mention_emits_candidate():
    alpha = _page("Alpha", body="Intro.\n\nWe use Beta for storage.\n")
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert len(candidates) == 1
    c = candidates[0]
    assert isinstance(c, LinkCandidate)
    assert c.source_title == "Alpha"
    assert c.source_path == alpha.path
    assert c.target_title == "Beta"
    assert c.target_path == beta.path
    assert c.term == "Beta"
    # "We use Beta for storage." is body line 3; "Beta" starts at column 8.
    assert c.line == 3
    assert c.column == 8
    assert "Beta" in c.snippet


# ---------------------------------------------------------------------------
# 2. Mentions already inside a Markdown link or wikilink are not re-proposed.
# ---------------------------------------------------------------------------


def test_mention_inside_markdown_link_not_emitted():
    alpha = _page("Alpha", body="See [Beta page](beta-page.md) for details.\n")
    beta = _page("Beta", path="beta-page.md")

    candidates = find_link_candidates([alpha, beta])

    assert candidates == []
    # The shared resolver already derives this link as an Extracted Reference.
    assert len(extract_references([alpha, beta])) == 1


def test_mention_inside_wikilink_not_emitted():
    alpha = _page("Alpha", body="See [[Beta]] for details.\n")
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert candidates == []
    assert len(extract_references([alpha, beta])) == 1


def test_mention_inside_external_link_label_not_reproposed():
    # Anchor text of ANY link construct is already linked prose; for internal
    # links the resolver derives the reference, and for external links there is
    # nothing to propose. "Beta" must not be re-proposed either way.
    alpha = _page("Alpha", body="[Beta site](https://example.com) is nice.\n")
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert candidates == []


# ---------------------------------------------------------------------------
# 3. Alias mentions resolve to their target page.
# ---------------------------------------------------------------------------


def test_alias_mention_emits_candidate_with_resolved_target():
    alpha = _page("Alpha", body="Config lives in The Vault.\n")
    beta = _page("Beta", aliases=["The Vault"])

    candidates = find_link_candidates([alpha, beta])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.target_title == "Beta"
    assert c.target_path == beta.path
    assert c.term == "The Vault"
    assert "The Vault" in c.snippet


# ---------------------------------------------------------------------------
# 4. Ambiguous terms are skipped silently.
# ---------------------------------------------------------------------------


def test_ambiguous_alias_not_emitted():
    alpha = _page("Alpha", body="We rely on Shared.\n")
    beta = _page("Beta", aliases=["Shared"])
    gamma = _page("Gamma", aliases=["Shared"])

    candidates = find_link_candidates([alpha, beta, gamma])

    assert candidates == []


def test_alias_colliding_with_title_is_ambiguous():
    alpha = _page("Alpha", body="We use Foo.\n")
    beta = _page("Beta", aliases=["Foo"])
    foo = _page("Foo")

    candidates = find_link_candidates([alpha, beta, foo])

    assert candidates == []


def test_duplicate_title_is_ambiguous_and_skipped():
    alpha = _page("Alpha", body="See Shared.\n")
    first = _page("Shared", path="shared-a.md")
    second = _page("Shared", path="shared-b.md")

    candidates = find_link_candidates([alpha, first, second])

    assert candidates == []


# ---------------------------------------------------------------------------
# 5. Self-mentions are not emitted.
# ---------------------------------------------------------------------------


def test_self_title_mention_not_emitted():
    alpha = _page("Alpha", body="Alpha is the entry point.\n")
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert candidates == []


def test_self_alias_mention_not_emitted():
    alpha = _page("Alpha", body="The Root service starts here.\n", aliases=["The Root"])
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert candidates == []


# ---------------------------------------------------------------------------
# 6. Determinism: stable ordering and whole-phrase matching.
# ---------------------------------------------------------------------------


def test_deterministic_ordering_across_input_order():
    pages = [
        _page("Alpha", body="Gamma then Beta then Gamma again.\n"),
        _page("Beta"),
        _page("Gamma"),
    ]

    first = find_link_candidates(list(pages))
    second = find_link_candidates(list(reversed(pages)))

    assert first == second
    keys = [
        (c.source_path, c.line, c.column, c.target_title, c.term) for c in first
    ]
    assert keys == sorted(keys)
    # Beta once, Gamma twice (two distinct locations).
    assert len(first) == 3
    assert [c.target_title for c in first] == ["Gamma", "Beta", "Gamma"]


def test_substring_is_not_matched():
    alpha = _page("Alpha", body="We use Betamax for video.\n")
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert candidates == []


def test_case_insensitive_mention_matches():
    alpha = _page("Alpha", body="we love beta tools.\n")
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert len(candidates) == 1
    assert candidates[0].target_title == "Beta"
    assert candidates[0].term == "Beta"


# ---------------------------------------------------------------------------
# 7. One candidate per mention location; inline code is masked.
# ---------------------------------------------------------------------------


def test_multiple_mentions_one_candidate_per_location():
    alpha = _page("Alpha", body="Beta is great. Beta also scales.\n")
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert len(candidates) == 2
    assert {c.line for c in candidates} == {1}
    assert candidates[0].column < candidates[1].column


def test_mention_in_inline_code_not_emitted():
    alpha = _page("Alpha", body="Run `Beta` to start.\n")
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert candidates == []


# ---------------------------------------------------------------------------
# 8. body_start_line offsets reported line numbers; long lines are trimmed.
# ---------------------------------------------------------------------------


def test_body_start_line_offsets_line_numbers():
    alpha = _page("Alpha", body="We use Beta.\n", body_start_line=10)
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert len(candidates) == 1
    assert candidates[0].line == 10


def test_long_line_snippet_is_trimmed():
    line = "x " * 120 + "Beta" + " y" * 120
    alpha = _page("Alpha", body=line + "\n")
    beta = _page("Beta")

    candidates = find_link_candidates([alpha, beta])

    assert len(candidates) == 1
    assert len(candidates[0].snippet) <= 162  # 160-char window + up to 2 ellipses
    assert "Beta" in candidates[0].snippet


# ---------------------------------------------------------------------------
# 9. The finder is pure: candidates are non-canonical and never enter the
#    Discovery Graph (the Extracted Reference set is unchanged by calling it).
# ---------------------------------------------------------------------------


def test_finder_does_not_publish_or_enter_discovery_graph():
    alpha = _page("Alpha", body="We use Beta and Gamma.\n")
    beta = _page("Beta")
    gamma = _page("Gamma")
    pages = [alpha, beta, gamma]

    before = extract_references(pages)
    candidates = find_link_candidates(pages)
    after = extract_references(pages)

    # No authored links exist, so the Discovery Graph has no extracted edges.
    assert before == []
    # The finder proposed candidates but did not publish anything.
    assert before == after
    assert candidates  # candidates exist, but remain non-canonical
    assert all(isinstance(c, LinkCandidate) for c in candidates)
