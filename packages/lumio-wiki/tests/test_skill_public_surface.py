"""Issue #110, AC3 + AC5: the packaged Agent Skill and coding-agent protocol
invoke only public CLI/Python behavior.

Retains the minimal trust guidance: no private MessagePack parsing,
Compiled Page paths and passages for citations, refusal without Evidence,
and optional private Source Artifacts that are not Evidence. Installed-wheel
journeys own first-run and canonical-graph guidance.
"""

from __future__ import annotations

import re
from pathlib import Path

from lumio_wiki.cli import _AGENTS_MD_SECTION
from lumio_wiki.skill import resolve_protocol_path, resolve_skill_path


def _skill_and_protocol() -> list[tuple[str, str]]:
    return [
        (path.name, path.read_text()) for path in (resolve_skill_path(), resolve_protocol_path())
    ]


def _generated_agents_md() -> str:
    """The AGENTS.md section that ``lumio-wiki setup`` writes into a project.

    ADR-0017 names the generated ``AGENTS.md`` as a parity surface alongside
    ``SKILL.md``, the detailed protocol, CLI help, and the usage docs, so the
    drift guard renders its template from the live CLI module (not a stale
    source-tree copy) and asserts against the formatted result. The plain
    local-setup render passes an empty S3 block (issue #161); the configured
    variant is asserted separately.
    """
    return _AGENTS_MD_SECTION.format(kb_path="/example/knowledge-base", s3_config="")


def test_skill_never_instructs_private_msgpack_parsing():
    """AC3: no instruction to parse/unpack the private MessagePack graph."""
    for name, text in _skill_and_protocol():
        low = text.lower()
        # No code-like parse instruction may appear.
        for forbidden in ("msgpack.unpack", "msgpack.pack", "import msgpack", ".msgpack"):
            assert forbidden not in low, f"{name} references private {forbidden!r}"
        # Every prose mention of msgpack must be a PROHIBITION, not a how-to.
        for line in text.splitlines():
            if "msgpack" in line.lower():
                ll = line.lower()
                assert any(
                    word in ll for word in ("never", "not", "public", "only", "no ", "without")
                ), f"{name} mentions MessagePack without a prohibition:\n  {line}"


def test_protocol_cites_compiled_page_paths_and_passages():
    """AC5: the protocol instructs citing paths AND passages."""
    protocol = resolve_protocol_path().read_text()
    low = protocol.lower()
    assert "passage" in low, "protocol must instruct citing supporting passages"
    assert "path" in low
    # The discovery scope (Extracted References) must be documented so agents
    # know body-link topology is available without LanceDB.
    assert "--scope discovery" in protocol or "scope discovery" in low


def test_protocol_instructs_not_covered_when_evidence_insufficient():
    """AC5: the protocol instructs reporting 'not covered' when Evidence is
    insufficient, and that connectivity cannot manufacture support."""
    protocol = resolve_protocol_path().read_text()
    low = protocol.lower()
    assert "not covered" in low
    assert "connectivity" in low or "topology" in low or "extracted reference" in low


# --- Issue #166: private Source Artifact guidance --------------------------


def _journey_surfaces() -> dict[str, str]:
    """The parity surfaces for the S3 journey contract (#166): the packaged
    skill + protocol, the usage docs, the generated AGENTS.md section, and the
    real-world trial README that documents the exact journey sequence."""
    root = Path(__file__).parents[3]
    return {
        **dict(_skill_and_protocol()),
        "docs/usage.md": (root / "docs" / "usage.md").read_text(encoding="utf-8"),
        "AGENTS.md": _generated_agents_md(),
        "examples/real-world-lumio-wiki/README.md": (
            root / "examples" / "real-world-lumio-wiki" / "README.md"
        ).read_text(encoding="utf-8"),
    }


def test_source_artifacts_documented_optional_private_and_not_evidence():
    """Issue #166: every public instruction surface documents that raw Source
    Artifacts are optional (retention disabled by default) and private, and
    that source inspection is authorized provenance review — not Evidence and
    not claim-level lineage."""
    from lumio_wiki.cli import _agents_md_s3_config

    surfaces = _journey_surfaces()
    # The generated AGENTS.md carries the contract in its configured S3 block.
    surfaces["AGENTS.md"] += _agents_md_s3_config(
        "s3://public-bucket/team-kb", "lancedb", "s3://private-bucket/team-kb"
    )
    for name, text in surfaces.items():
        flat = re.sub(r"\s+", " ", text.lower())
        assert "optional" in flat and "source artifact" in flat.replace("artifacts", "artifact"), (
            f"{name}: must document that raw Source Artifacts are optional"
        )
        assert "private" in flat, f"{name}: must document that raw Source Artifacts are private"
        assert (
            ("not evidence" in flat) or ("not claim-level" in flat) or ("not claim level" in flat)
        ), f"{name}: must document that source inspection is not Evidence / claim-level lineage"
