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

import pytest

from lumio_wiki.cli import main
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


def test_skill_documents_the_full_retrieval_ladder():
    """AC2: the skill documents Hot Index, Navigation Indexes, search, page,
    related, and bounded paths as the retrieval ladder."""
    skill = resolve_skill_path().read_text()
    for step in ("hot", "index", "search", "page", "related", "paths"):
        assert f"lumio-wiki {step}" in skill, f"ladder step {step!r} missing from SKILL.md"
    assert "retrieval ladder" in skill.lower()


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
