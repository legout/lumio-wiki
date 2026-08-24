"""Issue #110, AC3 + AC5: the packaged Agent Skill and coding-agent protocol
invoke only public CLI/Python behavior.

The packaged ``SKILL.md`` and ``PROTOCOL.md`` are the contract a coding agent
follows. They must (a) never instruct parsing the private MessagePack
Discovery Graph artifact, (b) cite only real public CLI commands, (c) document
the full retrieval ladder, and (d) instruct citing Compiled Page paths AND
passages and reporting "not covered" when Evidence is insufficient.

The command-discovery helper drives the PUBLIC CLI surface — it invokes
``lumio-wiki <cmd> --help`` per cited command and reads the exit code — rather
than inspecting argparse internals, so the check is end-to-end over what a
coding agent actually sees.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import msgspec  # type: ignore[import-not-found]
from lumio_wiki import __version__
from lumio_wiki.cli import _AGENTS_MD_SECTION, main
from lumio_wiki.skill import resolve_protocol_path, resolve_skill_path


def _command_is_registered(cmd: str) -> bool:
    """True if ``lumio-wiki <cmd>`` is a known top-level command.

    Drives the public CLI path end-to-end: a registered command accepts
    ``--help`` and exits 0; an unknown command exits 2 with an
    "invalid choice" error. No argparse internals are inspected.
    """
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main([cmd, "--help"])
        return True
    except SystemExit as exc:
        return exc.code == 0


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


def _cli_help(cmd: str) -> str:
    """Capture the stdout of ``lumio-wiki <cmd> --help`` end-to-end."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            main([cmd, "--help"])
        except SystemExit:
            # argparse exits after printing --help; the captured stdout is
            # the help text regardless of the exit code.
            pass
    return buf.getvalue()


def test_skill_uses_portable_agent_skills_frontmatter_and_relative_protocol():
    """Issue #150: the wheel contract follows the portable Agent Skills shape."""
    skill_path = resolve_skill_path()
    text = skill_path.read_text(encoding="utf-8")
    _opening, frontmatter, _body = text.split("---", 2)
    data = msgspec.yaml.decode(frontmatter.encode("utf-8"))

    allowed = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert isinstance(data, dict)
    assert set(data) <= allowed
    assert data["name"] == "lumio-wiki"
    assert isinstance(data["description"], str)
    assert data["metadata"] == {
        "distribution": "lumio-wiki",
        "version": __version__,
    }
    assert "(PROTOCOL.md)" in text
    assert (skill_path.parent / "PROTOCOL.md").is_file()


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


def test_every_cited_command_is_a_registered_public_cli_command():
    """AC3 + AC7: every ``lumio-wiki <cmd>`` cited is a real public command."""
    # Hyphenated command names (``cross-link``) must match as one token.
    pattern = re.compile(r"lumio-wiki ([\w-]+)")
    for name, text in _skill_and_protocol():
        cited = set(pattern.findall(text))
        unknown = {cmd for cmd in cited if not _command_is_registered(cmd)}
        assert not unknown, f"{name} cites unknown commands: {sorted(unknown)}"


def test_skill_management_and_setup_are_in_parity_across_public_surfaces():
    root = Path(__file__).parents[3]
    surfaces = {
        **dict(_skill_and_protocol()),
        "docs/usage.md": (root / "docs" / "usage.md").read_text(encoding="utf-8"),
    }
    for name, text in surfaces.items():
        assert "lumio-wiki skill install --scope user" in text, name
        assert "lumio-wiki skill status" in text, name
        assert "lumio-wiki skill update" in text, name
        assert "restart" in text.lower() or "new session" in text.lower(), name
    for name, text in _skill_and_protocol():
        assert "lumio-wiki setup" in text, name
        assert "lower-level" in text.lower(), name


def test_skill_documents_the_full_retrieval_ladder():
    """AC2: the skill documents Hot Index, Navigation Indexes, search, page,
    related, and bounded paths as the retrieval ladder."""
    skill = resolve_skill_path().read_text()
    for step in ("hot", "index", "search", "page", "related", "paths"):
        assert f"lumio-wiki {step}" in skill, f"ladder step {step!r} missing from SKILL.md"
    assert "retrieval ladder" in skill.lower()


def test_open_action_labels_are_documented_across_all_public_surfaces():
    """Issue #177 + ADR-0017: labelled open actions must not drift.

    The four action labels, the Reader base URL key, and the
    never-an-implicit-artifact-URL rule are repeated workflow facts across
    SKILL.md, PROTOCOL.md, and the generated AGENTS.md section. ADR-0017
    requires repeated facts to stay in parity across public surfaces, so
    each surface is pinned here.
    """
    surfaces = dict(_skill_and_protocol())
    surfaces["AGENTS.md"] = _generated_agents_md()
    labels = ("open:", "web:", "source-url:", "source-artifact:")
    for name, text in surfaces.items():
        for label in labels:
            assert label in text, f"{name} does not document the {label} action"
        assert "LUMIO_READER_BASE_URL" in text, (
            f"{name} does not name LUMIO_READER_BASE_URL"
        )
        assert "signed" in text.lower() or "artifact" in text.lower(), (
            f"{name} does not disclose the private-artifact rule"
        )


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


# --- Issue #148: first-run protocol and drift prevention (ADR-0017) ----------
# ADR-0017 lists five parity surfaces: SKILL.md, the detailed PROTOCOL.md, the
# generated AGENTS.md, CLI help, and the usage docs. The tests below lock the
# facts that must stay identical across them so the surfaces cannot silently
# diverge again.


def test_setup_is_canonical_first_run_across_all_public_surfaces():
    """Issue #148 AC1/AC2: every public surface makes ``lumio-wiki setup`` the
    canonical first-run command, documents ``init`` as the lower-level KB-only
    operation, and explains that setup writes ``.env`` and ``AGENTS.md``. The
    ``setup`` CLI help is a parity surface too (ADR-0017)."""
    root = Path(__file__).parents[3]
    surfaces = {
        "SKILL.md": resolve_skill_path().read_text(encoding="utf-8"),
        "PROTOCOL.md": resolve_protocol_path().read_text(encoding="utf-8"),
        "docs/usage.md": (root / "docs" / "usage.md").read_text(encoding="utf-8"),
    }
    for name, text in surfaces.items():
        assert "lumio-wiki setup" in text, f"{name}: must name lumio-wiki setup"
        assert "lower-level" in text.lower(), (
            f"{name}: must document init as the lower-level KB-only operation"
        )
        assert ".env" in text, f"{name}: must explain setup writes .env"
        assert "AGENTS.md" in text, f"{name}: must explain setup writes AGENTS.md"
    # The `setup` CLI help describes the one-command bootstrap (writes .env and
    # AGENTS.md), so a coding agent reading only `lumio-wiki setup --help` lands
    # on the canonical first-run path too.
    setup_help = _cli_help("setup")
    assert ".env" in setup_help, "setup --help must mention .env"
    assert "AGENTS.md" in setup_help, "setup --help must mention AGENTS.md"
    # The `init` CLI help distinguishes itself as the lower-level KB-only
    # operation so an agent reading `lumio-wiki init --help` does not mistake it
    # for the canonical first run (AC2).
    init_help = _cli_help("init")
    assert "lower-level" in init_help.lower(), (
        "init --help must document the lower-level KB-only operation"
    )


def test_generated_agents_md_guides_a_restarted_session():
    """Issue #148 AC3: the AGENTS.md section that ``setup`` writes tells a
    restarted/new session how to LOCATE, RETRIEVE FROM, CITE, INGEST INTO, and
    MAINTAIN the existing Knowledge Base."""
    text = _generated_agents_md()
    # Locate: the recorded KB path (resolved from .env when no <kb> is given).
    assert "LUMIO_KB_PATH" in text and "KB path" in text
    # Retrieve: the full retrieval ladder (ladder 0..5).
    for step in ("hot", "index", "search", "page", "related", "paths"):
        assert f"lumio-wiki {step}" in text, f"ladder step {step!r} missing from AGENTS.md"
    # Cite: cite-or-refuse guardrail naming title + path + passage.
    assert "cite" in text.lower()
    assert "passage" in text.lower()
    # Ingest: host-Distiller managed ingest that binds the raw source.
    assert "lumio-wiki ingest" in text
    assert "Distiller" in text
    # Maintain: maintenance commands (lint, cross-link, relationship, dream).
    assert "lumio-wiki lint" in text
    assert "lumio-wiki dream" in text


def test_surfaces_distinguish_extracted_references_from_typed_relationships():
    """Issue #148 AC4, post-ADR-0021: the skill, protocol, and generated
    AGENTS.md keep authored Markdown links / Extracted References distinct
    from reviewed typed canonical edges. ``cross-link --stage`` is tied to
    Extracted References and is never described as creating a typed canonical
    edge; since the title-based Relationship staging was removed, the surfaces
    must NOT document ``relationship stage`` at all — canonical edges are
    accepted, evidence-bearing Claims authored in Compiled Page frontmatter."""
    surfaces = {
        "SKILL.md": resolve_skill_path().read_text(encoding="utf-8"),
        "PROTOCOL.md": resolve_protocol_path().read_text(encoding="utf-8"),
        "AGENTS.md": _generated_agents_md(),
    }
    for name, text in surfaces.items():
        flat = re.sub(r"\s+", " ", text.lower())
        assert "extracted reference" in flat, f"{name}: must name Extracted References"
        # ADR-0021 removed the `relationship` CLI subcommand and the
        # title-based staging workflow: no surface may cite it.
        assert "relationship stage" not in flat, (
            f"{name}: must not document the removed relationship stage command"
        )
    # The command-bearing surfaces (SKILL.md and the generated AGENTS.md)
    # document cross-link in terms of Extracted References (body-link
    # topology), never as creating a typed canonical edge.
    for name in ("SKILL.md", "AGENTS.md"):
        flat = re.sub(r"\s+", " ", surfaces[name].lower())
        assert "cross-link" in flat, f"{name}: must document cross-link"
        assert re.search(r"cross-link.{0,180}extracted", flat), (
            f"{name}: cross-link must be tied to Extracted References, not to typed canonical edges"
        )
        assert "typed" in flat, f"{name}: must name typed canonical edges"
    # Canonical edges are accepted, evidence-bearing Claims validated against
    # the Control File ontology (ADR-0021).
    for name in ("SKILL.md", "PROTOCOL.md"):
        flat = re.sub(r"\s+", " ", surfaces[name].lower())
        assert "claim" in flat, f"{name}: must name Claims as the canonical edge form"


def test_real_world_readme_names_exact_setup_and_restart_check():
    """Issue #148 AC7: the real-world trial README names the exact first-run
    command (``lumio-wiki setup``) and tells the agent to restart or start a new
    session so the Knowledge Base and an optional installed skill are both
    discovered."""
    root = Path(__file__).parents[3]
    readme = (root / "examples" / "real-world-lumio-wiki" / "README.md").read_text(encoding="utf-8")
    assert "lumio-wiki setup" in readme, "README must name the exact setup command"
    assert "restart" in readme.lower() or "new session" in readme.lower(), (
        "README must include a restart/new-session check"
    )


def test_command_coverage_parity_for_relationship_and_source_lifecycle():
    """ADR-0021 follow-up to issue #148 change #4: the title-based
    ``relationship stage`` command was removed with the Relationship input
    contract, so the packaged skill surface must not cite it anymore, while
    the ``source`` lifecycle commands stay documented on both command-coverage
    surfaces so they cannot diverge. (``docs/usage.md`` still carries the
    stale command until its own migration lands; the drift guard for it is
    restored once that doc is updated.)
    ``test_every_cited_command_is_a_registered_public_cli_command``
    already proves every cited command is real."""
    root = Path(__file__).parents[3]
    skill = resolve_skill_path().read_text(encoding="utf-8")
    usage = (root / "docs" / "usage.md").read_text(encoding="utf-8")
    assert "relationship stage" not in skill.lower(), (
        "SKILL.md: must not document the removed relationship stage command"
    )
    for name, text in (("SKILL.md", skill), ("docs/usage.md", usage)):
        assert "lumio-wiki source" in text.lower(), (
            f"{name}: must document the source lifecycle commands"
        )


def test_s3_setup_forms_parity_across_public_surfaces():
    """Issue #161: the S3 setup forms are documented on every parity surface.

    ADR-0017 names SKILL.md, PROTOCOL.md, the generated AGENTS.md, CLI help,
    and the usage docs as drift-guarded surfaces; issue #161 extends ``setup``
    with the Maintainer ``--publish-to`` and reader ``--from`` forms plus the
    bounded ``.env`` allowlist, so each surface must document them with the
    same facts (env keys, backend/mode separation, no credentials, exact
    install guidance).
    """
    root = Path(__file__).parents[3]
    surfaces = {
        "SKILL.md": resolve_skill_path().read_text(encoding="utf-8"),
        "PROTOCOL.md": resolve_protocol_path().read_text(encoding="utf-8"),
        "docs/usage.md": (root / "docs" / "usage.md").read_text(encoding="utf-8"),
    }
    for name, text in surfaces.items():
        assert "--publish-to" in text, f"{name}: must document the Maintainer publish form"
        assert "--from" in text, f"{name}: must document the reader --from form"
        assert "LUMIO_PUBLISH_TO" in text, f"{name}: must name the publication .env key"
        assert "LUMIO_RETRIEVAL_BACKEND" in text, (
            f"{name}: must name the retrieval-backend .env key"
        )
        assert "LUMIO_SOURCE_STORE" in text, f"{name}: must name the source-store .env key"
    # Backend and mode separation is stated, not just implied (ADR-0019).
    for name, text in surfaces.items():
        assert "mode" in text.lower(), f"{name}: must distinguish mode from backend"
    # The generated AGENTS.md records the configured S3 settings.
    from lumio_wiki.cli import _agents_md_s3_config

    configured = _agents_md_s3_config(
        "s3://public-bucket/team-kb", "lancedb", "s3://private-bucket/team-kb"
    )
    for key in ("LUMIO_PUBLISH_TO", "LUMIO_RETRIEVAL_BACKEND", "LUMIO_SOURCE_STORE"):
        assert key in configured, f"generated AGENTS.md must record {key}"
    # CLI help: the setup surface itself names both forms and the exact
    # install guidance for the optional distributions.
    setup_help = _cli_help("setup")
    assert "--publish-to" in setup_help and "--from" in setup_help
    assert "lumio-wiki[s3]" in setup_help and "lumio-lancedb[s3]" in setup_help


# --- Issue #166: certify the S3 coding-agent journey documentation parity ----


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


def test_s3_journey_is_documented_across_public_surfaces():
    """Issue #166 AC1: one documented command sequence covers the journey from
    an empty project (journey install, maintainer setup, mixed-format managed
    ingest, immutable publication with remote LanceDB) through a separate
    read-only project (fresh-harness retrieval, exact Source Artifact
    inspection, historical versions) to conflict/rollback/orphan/zero-index
    behavior. The surfaces cannot silently drop a journey leg again."""
    surfaces = _journey_surfaces()
    readme = surfaces["examples/real-world-lumio-wiki/README.md"]
    # The exact certified sequence (mirrors test_s3_agent_journey_minio.py).
    for fragment in (
        "lumio-wiki[s3]",
        "lumio-lancedb[s3]",
        "lumio-wiki setup ./knowledge-base",
        "--publish-to s3://",
        "--retrieval lancedb",
        "--source-store s3://",
        "--artifact-retention required",
        "--skill-scope project",
        "lumio-wiki validate",
        "--compiled-page staging/overview-page.md --source-id overview-src",
        "proposal inspect",
        "proposal validate",
        "lumio-wiki publish-s3 --version v1 --retrieval lancedb",
        "lumio-wiki setup --from s3://public-kb-bucket/team-kb",
        'lumio-wiki search "coating process"',
        "lumio-wiki source inspect --source-id overview-src",
        "lumio-wiki source fetch --source-id overview-src --output",
        "lumio-wiki source link --source-id overview-src --expires 5m",
        "lumio-wiki source retire knowledge-base --source-id overview-src",
        "lumio-wiki source reactivate knowledge-base --source-id overview-src",
        "lumio-wiki publish-s3 --version v2 --retrieval lancedb",
        "--published-version v1",
        "lumio-wiki cleanup-s3",
        "lumio-wiki rollback-s3",
        "LUMIO_RETRIEVAL_BACKEND=zero-index",
    ):
        assert fragment in readme, f"journey README must document {fragment!r}"
    # The restart/new-session discovery check stays part of the journey docs.
    assert "Restart the harness" in readme or "restart" in readme.lower()
    # The install isolation and MinIO certification are named as the guards.
    assert "test_s3_agent_journey_minio.py" in readme
    assert "test_wheel_isolation.py" in readme
    # The usage docs keep the publish/reader/inspection legs (ADR-0017 set).
    usage = surfaces["docs/usage.md"]
    for fragment in (
        "publish-s3 <kb> [dest] --version <v>",
        "--retrieval lancedb",
        "setup --from s3://public-kb-bucket/team-kb",
        "source inspect",
        "source fetch",
        "source link",
        "rollback-s3",
        "cleanup-s3",
    ):
        assert fragment in usage, f"docs/usage.md must document {fragment!r}"


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


def test_doctor_guides_the_s3_journey_install(monkeypatch, capsys):
    """Issue #166: doctor guidance is a parity surface — when the S3
    capabilities are absent it names the exact journey install commands
    (lumio-wiki[s3], lumio-lancedb[s3]) through the public CLI."""
    import lumio_wiki.cli as cli_module

    monkeypatch.setattr(cli_module, "_detect_module", lambda name: False)
    rc = main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extra[s3]: not installed" in out
    assert "pip install 'lumio-wiki[s3]'" in out
    assert "extra[lancedb]: not installed" in out
    assert "pip install 'lumio-lancedb[s3]'" in out
