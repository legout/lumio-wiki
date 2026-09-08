"""Issue #98: the ``lumio-wiki`` CLI dispatches every public Knowledge Base
operation through its command surface.

These tests drive the CLI in-process (``lumio_wiki.cli.main``) so they are
fast and deterministic. The authoritative isolated-built-wheel proof lives in
``test_wheel_isolation.py``; this file locks in the command wiring, exit
codes, and output contracts against the package's own test fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import lumio_wiki as lw
import pytest  # type: ignore[import-not-found]
from lumio_wiki import GRAPH_ARTIFACT_FILENAME
from lumio_wiki.cli import default_index_dir, main

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

# Authored Compiled Page Markdown the host coding agent would produce.
PAGE_MD = textwrap.dedent(
    """\
    ---
    title: "CLI Page"
    aliases: []
    tags:
      - "cli"
    summary: "Authored through the lumio-wiki CLI."
    lifecycle: "draft"
    visibility: "internal"
    sources:
      - id: "cli"
        title: "CLI test source"
    synthetic: false
    ---

    # CLI Page

    Body authored by the host agent.
    """
)


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    """Copy the valid fixture into a writable Knowledge Base root."""
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", root)
    return root


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    """Write a test Knowledge Source file."""
    path = tmp_path / "source.md"
    path.write_text(PAGE_MD, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_refuses_existing_knowledge_base(tmp_path: Path):
    target = tmp_path / "existing"
    main(["init", str(target)])
    rc = main(["init", str(target)])
    assert rc == 2  # CliError exit code


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_invalid_knowledge_base_returns_1(tmp_path: Path):
    root = tmp_path / "bad"
    shutil.copytree(FIXTURES / "invalid", root)
    rc = main(["validate", str(root)])
    assert rc == 1


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_no_matches_returns_0(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["search", str(kb_root), "zzzznomatchzzzz"])
    assert rc == 0
    assert "No pages matched" in capsys.readouterr().out


def test_search_rejects_unknown_mode(kb_root: Path):
    # argparse ``choices`` validation exits 2 before any retrieval runs.
    with pytest.raises(SystemExit):
        main(["search", str(kb_root), "warranty", "--mode", "bm25"])


def test_search_semantic_guard_when_lancedb_unavailable(
    monkeypatch: pytest.MonkeyPatch, kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """--mode semantic degrades to an install-hint CliError (exit 2) instead of
    an opaque ImportError when lumio-lancedb is absent (ADR-0010)."""
    from lumio_wiki import retrieval_eval

    monkeypatch.setattr(retrieval_eval, "lancedb_available", lambda: False)
    rc = main(["search", str(kb_root), "warranty", "--mode", "semantic"])
    assert rc == 2
    assert "lumio-lancedb" in capsys.readouterr().err


def test_search_semantic_needs_embedder_hint(
    monkeypatch: pytest.MonkeyPatch, kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """With lumio-lancedb present but no embedder available, --mode semantic
    reports the embedder install hint (exit 2) rather than a bare ImportError."""
    import importlib.util

    from lumio_wiki import retrieval_eval

    monkeypatch.setattr(retrieval_eval, "lancedb_available", lambda: True)
    # Force the local sentence-transformers path to look absent and clear the
    # provider env so neither embedder source resolves.
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "sentence_transformers":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    for var in (
        "LUMIO_PROVIDER_BASE_URL",
        "LUMIO_PROVIDER_API_KEY",
        "LUMIO_EMBEDDING_MODEL",
        "LUMIO_PROVIDER_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    rc = main(["search", str(kb_root), "warranty", "--mode", "hybrid"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "embedder" in err
    assert "lumio-lancedb[embeddings]" in err


# ---------------------------------------------------------------------------
# eval (issue #138 / #158)
# ---------------------------------------------------------------------------


def _eval_gold_set(kb_root: Path) -> Path:
    """Write a minimal gold set referencing the valid fixture's pages."""
    gold = kb_root / "gold_set.yaml"
    gold.write_text(
        'name: "valid-fixture"\n'
        "ks: [1, 3]\n"
        "queries:\n"
        '  - query: "technology"\n'
        "    relevant:\n"
        '      - "Technology Stack"\n',
        encoding="utf-8",
    )
    return gold


def test_eval_semantic_default_uses_hash_embedder(
    monkeypatch: pytest.MonkeyPatch, kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """--semantic without --model (and no provider env) keeps the hermetic
    DeterministicHashEmbedder, identified by name in the JSON report (#158)."""
    for var in (
        "LUMIO_PROVIDER_BASE_URL",
        "LUMIO_PROVIDER_API_KEY",
        "LUMIO_EMBEDDING_MODEL",
        "LUMIO_PROVIDER_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    gold = _eval_gold_set(kb_root)
    rc = main(["eval", str(kb_root), "--gold-set", str(gold), "--semantic", "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["embedder"] == "lumio-eval-deterministic-hash"


def test_eval_semantic_model_resolves_real_embedder(
    monkeypatch: pytest.MonkeyPatch, kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """--semantic --model routes through the same _resolve_embedder as `search`
    and surfaces the resolved model name (#158)."""
    from lumio_wiki import cli
    from lumio_wiki.embeddings import EmbeddingModelInfo

    class _FakeEmbedder:
        def __init__(self) -> None:
            self._info = EmbeddingModelInfo("fake-model", 8)

        def embed(self, texts: list[str]):
            return [[0.0] * 8 for _ in texts]

        @property
        def model_info(self) -> EmbeddingModelInfo:
            return self._info

    captured: dict[str, str | None] = {}

    def fake_resolve(model: str | None):
        captured["model"] = model
        return _FakeEmbedder()

    monkeypatch.setattr(cli, "_resolve_embedder", fake_resolve)
    gold = _eval_gold_set(kb_root)
    rc = main(
        [
            "eval",
            str(kb_root),
            "--gold-set",
            str(gold),
            "--semantic",
            "--model",
            "all-MiniLM-L6-v2",
            "--json",
        ]
    )
    assert rc == 0
    assert captured["model"] == "all-MiniLM-L6-v2"
    assert json.loads(capsys.readouterr().out)["embedder"] == "fake-model"


def test_eval_semantic_provider_env_routes_to_resolver(
    monkeypatch: pytest.MonkeyPatch, kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """Provider env (LUMIO_PROVIDER_*) without --model still resolves a real
    embedder — provider-first parity with `search` (#158)."""
    from lumio_wiki import cli
    from lumio_wiki.embeddings import EmbeddingModelInfo

    class _FakeEmbedder:
        def __init__(self) -> None:
            self._info = EmbeddingModelInfo("provider-model", 8)

        def embed(self, texts: list[str]):
            return [[0.0] * 8 for _ in texts]

        @property
        def model_info(self) -> EmbeddingModelInfo:
            return self._info

    monkeypatch.setattr(cli, "_resolve_embedder", lambda model: _FakeEmbedder())
    monkeypatch.setenv("LUMIO_PROVIDER_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("LUMIO_PROVIDER_API_KEY", "key")
    gold = _eval_gold_set(kb_root)
    rc = main(["eval", str(kb_root), "--gold-set", str(gold), "--semantic", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["embedder"] == "provider-model"


def test_resolve_embedder_prefers_provider_over_local(
    monkeypatch: pytest.MonkeyPatch,
):
    """``_resolve_embedder`` selects ``_ProviderEmbedder`` over local
    sentence-transformers when provider env + model are set (provider-first
    precedence, identical to `search`) — issue #158 acceptance criterion."""
    import sys
    import types
    from unittest.mock import MagicMock

    from lumio_wiki import cli

    # Stub the openai module so _ProviderEmbedder never opens a real client /
    # socket; works whether or not the optional [llm] extra is installed.
    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = types.SimpleNamespace(
        data=[types.SimpleNamespace(embedding=[0.0] * 8, index=0)]
    )
    fake_openai = types.ModuleType("openai")
    fake_openai.__dict__["OpenAI"] = MagicMock(return_value=fake_client)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    monkeypatch.setenv("LUMIO_PROVIDER_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("LUMIO_PROVIDER_API_KEY", "key")
    embedder = cli._resolve_embedder("provider-model-id")
    assert isinstance(embedder, cli._ProviderEmbedder)
    assert embedder.model_info.name == "provider-model-id"
    assert embedder.model_info.dimension == 8


def test_eval_semantic_model_missing_extra_guidance(
    monkeypatch: pytest.MonkeyPatch, kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """--model with sentence-transformers absent and no provider env yields the
    same actionable embedder hint as `search` (exit 2), not a bare ImportError."""
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "sentence_transformers":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    for var in (
        "LUMIO_PROVIDER_BASE_URL",
        "LUMIO_PROVIDER_API_KEY",
        "LUMIO_EMBEDDING_MODEL",
        "LUMIO_PROVIDER_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    gold = _eval_gold_set(kb_root)
    rc = main(
        [
            "eval",
            str(kb_root),
            "--gold-set",
            str(gold),
            "--semantic",
            "--model",
            "all-MiniLM-L6-v2",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "embedder" in err
    assert "lumio-lancedb[embeddings]" in err


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


def test_page_unknown_title_returns_1(kb_root: Path):
    rc = main(["page", str(kb_root), "Nonexistent Page"])
    assert rc == 1


# ---------------------------------------------------------------------------
# related + paths (graph traversal)
# ---------------------------------------------------------------------------


def test_paths_no_path_returns_1(kb_root: Path):
    # A non-existent source title has no outgoing edges, so no path is found.
    rc = main(["paths", str(kb_root), "Nonexistent Source", "Architecture"])
    assert rc == 1


# ---------------------------------------------------------------------------
# issue #149 — managed host-Distiller ingest (binds the original source +
# authored page under one stable source identity).
# ---------------------------------------------------------------------------


_MANAGED_PAGE = (
    "---\n"
    'title: "Managed Impact Report"\n'
    "aliases: []\n"
    'tags:\n  - "report"\n'
    'summary: "Authored from the PDF."\n'
    'lifecycle: "draft"\n'
    'visibility: "internal"\n'
    "sources:\n"
    '  - id: "annual-impact-report"\n'
    '    title: "2025 Impact Report"\n'
    "synthetic: false\n"
    "---\n\n"
    "# Managed Impact Report\n\n"
    "Body authored from the original PDF.\n"
)


def test_managed_ingest_binds_source_and_authored_page(
    kb_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    source = tmp_path / "2025-impact-report.pdf"
    source.write_bytes(b"%PDF-1.4 original impact report bytes")
    page = tmp_path / "authored.md"
    page.write_text(_MANAGED_PAGE, encoding="utf-8")

    rc = main(
        [
            "ingest",
            str(kb_root),
            str(source),
            "--compiled-page",
            str(page),
            "--source-id",
            "annual-impact-report",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out
    assert "source_id:      annual-impact-report" in out
    assert "converted_by:   liteparse" in out
    assert "source_hash:" in out
    # The private Source identity was registered with an immutable version.
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    registered = store.source_registry.get("annual-impact-report")
    assert registered.status == "active"
    assert len(registered.versions) == 1
    import hashlib

    assert registered.versions[0].content_hash == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("suffix", "expected_content_type", "expected_converter"),
    [
        (".pdf", "application/pdf", "liteparse"),
        (
            ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "anydoc",
        ),
        (".html", "text/html", "markitdown"),
        (".md", "text/markdown", "markdown"),
        (".txt", "text/plain", "text"),
    ],
)
def test_managed_ingest_infers_original_content_type(
    kb_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
    expected_content_type: str,
    expected_converter: str,
):
    source = tmp_path / f"report{suffix}"
    source.write_bytes(b"original source bytes")
    page = tmp_path / "authored.md"
    page.write_text(_MANAGED_PAGE, encoding="utf-8")

    rc = main(
        [
            "ingest",
            str(kb_root),
            str(source),
            "--compiled-page",
            str(page),
            "--source-id",
            "annual-impact-report",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert f"content_type:    {expected_content_type}" in out
    assert f"converted_by:   {expected_converter}" in out


def test_managed_ingest_compiled_page_and_source_id_are_required_together(
    kb_root: Path, tmp_path: Path
):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4")
    page = tmp_path / "authored.md"
    page.write_text(_MANAGED_PAGE, encoding="utf-8")

    # Only --compiled-page (no --source-id) must fail.
    rc = main(["ingest", str(kb_root), str(source), "--compiled-page", str(page)])
    assert rc != 0
    # Only --source-id (no --compiled-page) must fail.
    rc = main(["ingest", str(kb_root), str(source), "--source-id", "annual-impact-report"])
    assert rc != 0
    # Nothing was staged by the rejected invocations.
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store.list() == []


def test_managed_ingest_missing_source_id_in_page_blocks_with_actionable_error(
    kb_root: Path, tmp_path: Path
):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4")
    page = tmp_path / "authored.md"
    # The page cites a DIFFERENT source id than the one passed on the CLI.
    page.write_text(
        _MANAGED_PAGE.replace("annual-impact-report", "some-other-id"), encoding="utf-8"
    )

    rc = main(
        [
            "ingest",
            str(kb_root),
            str(source),
            "--compiled-page",
            str(page),
            "--source-id",
            "annual-impact-report",
        ]
    )
    assert rc != 0


def test_managed_ingest_inspect_distinguishes_provenance_from_authored_content(
    kb_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    source = tmp_path / "2025-impact-report.pdf"
    source.write_bytes(b"%PDF-1.4 original")
    page = tmp_path / "authored.md"
    page.write_text(_MANAGED_PAGE, encoding="utf-8")
    main(
        [
            "ingest",
            str(kb_root),
            str(source),
            "--compiled-page",
            str(page),
            "--source-id",
            "annual-impact-report",
        ]
    )
    capsys.readouterr()  # drain ingest output.
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    rc = main(["proposal", "inspect", str(kb_root), pid])
    assert rc == 0
    out = capsys.readouterr().out
    # Raw-source provenance block.
    assert "source_id:       annual-impact-report" in out
    assert "source_hash:" in out
    assert "converted_by:    liteparse" in out
    # Authored Compiled Page content (the diff).
    assert "Body authored from the original PDF" in out


def test_proposal_inspect_json_redacts_private_reviewed_metadata(
    kb_root: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
):
    # Plan 02 / P4 remediation (BLOCKER): ``proposal inspect --json`` used to
    # serialize the complete IngestProposal, exposing the private reviewed
    # preconditions (affected paths, roles, SHA-256 byte digests) and the
    # reviewed content identity. Those are DURABLE metadata consumed by
    # publish — never an inspection surface (see ``PathPrecondition``) — so
    # the JSON view redacts both fields while staying valid JSON with every
    # public field intact.
    main(["ingest", str(kb_root), str(source_file)])
    capsys.readouterr()  # drain ingest output.
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    durable = store.get(pid)
    assert durable is not None
    assert durable.preconditions and durable.reviewed_identity

    rc = main(["proposal", "inspect", str(kb_root), pid, "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)  # valid JSON
    # No private metadata key survives anywhere in the payload: the two
    # reviewed-state fields themselves, and the ``role``/``digest`` field
    # names only precondition records use (public ``path``/``kind`` keys
    # exist on OkfImportDiagnostic and stay).
    keys: set[str] = set()
    stack: list[object] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            keys.update(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    assert not keys & {"preconditions", "reviewed_identity", "role", "digest"}
    # And none of their values leak through any other field: reviewed byte
    # digests, the identity, and precondition roles are all absent.
    for item in durable.preconditions:
        if item.digest:
            assert item.digest not in out
        assert json.dumps(item.role) not in out
    assert durable.reviewed_identity not in out
    # The public inspection surface is unchanged.
    assert payload["id"] == pid
    assert payload["status"] == "staged"
    assert payload["proposed_pages"]
    assert payload["diff"]
    assert payload["validation_report"]
    assert payload["provenance"]["source_hash"]


def test_publish_writes_page_and_marks_terminal(
    kb_root: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
):
    main(["ingest", str(kb_root), str(source_file)])
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    rc = main(["publish", str(kb_root), pid])
    assert rc == 0
    # The page was actually written to the KB root.
    assert (kb_root / "cli_page.md").is_file()
    # The proposal is now terminal.
    store2 = lw.IngestStore(kb_root / ".lumio" / "ingest")
    published = store2.get(pid)
    assert published is not None
    assert published.status == "published"
    # KB remains valid after publish.
    assert lw.validate(kb_root).is_valid


def test_discard_marks_proposal_terminal(kb_root: Path, source_file: Path):
    main(["ingest", str(kb_root), str(source_file)])
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    rc = main(["discard", str(kb_root), pid])
    assert rc == 0
    store2 = lw.IngestStore(kb_root / ".lumio" / "ingest")
    discarded = store2.get(pid)
    assert discarded is not None
    assert discarded.status == "discarded"


def test_publish_unknown_proposal_returns_1(kb_root: Path):
    rc = main(["publish", str(kb_root), "nonexistent-id"])
    assert rc == 1


def test_lumio_dot_dir_does_not_pollute_fingerprint(kb_root: Path, source_file: Path):
    """The .lumio/ ingest store must not appear in the KB fingerprint."""
    fp_before = lw.fingerprint_sources(kb_root).digest
    main(["ingest", str(kb_root), str(source_file)])
    fp_after = lw.fingerprint_sources(kb_root).digest
    assert fp_before == fp_after


# ---------------------------------------------------------------------------
# ingest --distiller (issue #101)
# ---------------------------------------------------------------------------


def test_ingest_distiller_llm_without_extra_fails_actionably(
    kb_root: Path, source_file: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
):
    """AC4: requesting the llm distiller without the extra names the exact install.

    The base install (no ``openai`` module) must fail with the exact install
    command before any provider configuration is validated, so a missing extra
    is never masked by a missing-config error (ADR-0010, PRD user story 18).
    """
    # Ensure the extra is reported as absent (the dev env may or may not have
    # openai installed; force the missing-extra path deterministically).
    monkeypatch.setattr("lumio_wiki.cli._detect_module", lambda name: False)
    monkeypatch.setenv("LUMIO_PROVIDER_MODEL", "fake-model")
    rc = main(["ingest", str(kb_root), str(source_file), "--distiller", "llm"])
    assert rc == 2  # CliError exit code
    err = capsys.readouterr().err
    assert "pip install 'lumio-wiki[llm]'" in err, (
        "the missing-extra error must name the exact install command"
    )


def test_ingest_distiller_llm_with_fake_provider_stages_proposal(
    kb_root: Path,
    source_file: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    """AC2/AC3: ``--distiller llm`` with a fake provider stages a proposal."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    provider_md = (
        "---\n"
        'title: "LLM CLI Page"\n'
        "aliases: []\n"
        "tags:\n"
        '  - "llm"\n'
        'summary: "Distilled via the llm extra."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        '  - id: "llm"\n'
        '    title: "LLM source"\n'
        "---\n\n# LLM CLI Page\n\nBody.\n"
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=provider_md))]
    )

    # Bypass real OpenAI client construction by patching the Distiller factory.
    import lumio_wiki

    real_openai_distiller = lumio_wiki.OpenAIDistiller

    class _StubDistiller(real_openai_distiller):
        def __init__(self, **kwargs):
            filtered = {
                k: v for k, v in kwargs.items() if k not in {"model", "base_url", "api_key"}
            }
            super().__init__(model="fake", client=fake_client, **filtered)

    monkeypatch.setattr(lumio_wiki, "OpenAIDistiller", _StubDistiller)
    monkeypatch.setenv("LUMIO_PROVIDER_MODEL", "fake-model")
    rc = main(["ingest", str(kb_root), str(source_file), "--distiller", "llm"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out
    assert "LLM CLI Page" in out


# ---------------------------------------------------------------------------
# skill subcommands
# ---------------------------------------------------------------------------


def test_skill_install_refuses_overwrite_without_flag(tmp_path: Path):
    dest = tmp_path / "skills" / "lumio-wiki"
    main(["skill", "install", "--agent", "pi", "--dest", str(dest)])
    rc = main(["skill", "install", "--agent", "pi", "--dest", str(dest)])
    assert rc != 0


def test_skill_install_overwrite_replaces_existing(tmp_path: Path):
    dest = tmp_path / "skills" / "lumio-wiki"
    main(["skill", "install", "--agent", "pi", "--dest", str(dest)])
    rc = main(["skill", "install", "--agent", "pi", "--dest", str(dest), "--overwrite"])
    assert rc == 0


def test_unsupported_agent_is_rejected():
    with pytest.raises(SystemExit):
        main(["skill", "install", "--agent", "unsupported-agent"])


# ---------------------------------------------------------------------------
# Issue #110: the zero-index retrieval ladder — graph bounds + truthful trace,
# Hot Index / Navigation Index surfaces, and graceful graph recovery.
# ---------------------------------------------------------------------------


# A categorized KB whose canonical graph is the chain
# Lumio Overview -> Architecture -> Technology Stack (accepted entity Claims;
# the title-based relationship frontmatter input is gone, ADR-0021).
_GRAPH_CONTROL = """\
version: 2
mode: "categorized"
categories:
  - name: concepts
ontology:
  entity_types:
    concept: {}
  predicates:
    uses:
      subject_types: [concept]
      object_types: [concept]
"""


def _graph_page(title: str, *, body: str, claims_yaml: str = "") -> str:
    slug = title.lower().replace(" ", "-")
    claims = f"claims:\n{claims_yaml}" if claims_yaml else ""
    return (
        "---\n"
        f'id: "entity:{slug}"\n'
        f'title: "{title}"\n'
        "entity_types:\n"
        "  - concept\n"
        'tags:\n  - "test"\n'
        f'summary: "{title} summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        "sources:\n"
        f'  - id: "src-{slug}"\n'
        f'    title: "{title} Source"\n'
        f"{claims}"
        "---\n\n"
        f"# {title}\n\n## Overview\n\n{body}\n"
    )


@pytest.fixture
def graph_kb(tmp_path: Path) -> Path:
    """A claim-bearing KB with the canonical chain
    Lumio Overview -> Architecture -> Technology Stack."""
    root = tmp_path / "graph-kb"
    root.mkdir(parents=True)
    (root / "lumio.yaml").write_text(_GRAPH_CONTROL, encoding="utf-8")
    pages = {
        "concepts/overview.md": _graph_page(
            "Lumio Overview",
            body="Lumio is a deployable chat platform for trusted knowledge and data.",
            claims_yaml=(
                "  - id: claim:lumio-overview-uses-architecture\n"
                "    predicate: uses\n"
                '    object: "entity:architecture"\n'
                "    status: accepted\n"
                "    evidence:\n"
                '      - section: "Overview"\n'
            ),
        ),
        "concepts/architecture.md": _graph_page(
            "Architecture",
            body=(
                "Lumio is built as a modular monolith with a framework-independent "
                "Core SDK. Lumio uses LanceDB for the derived lexical index."
            ),
            claims_yaml=(
                "  - id: claim:architecture-uses-technology-stack\n"
                "    predicate: uses\n"
                '    object: "entity:technology-stack"\n'
                "    status: accepted\n"
                "    evidence:\n"
                '      - section: "Overview"\n'
            ),
        ),
        "concepts/technology.md": _graph_page(
            "Technology Stack",
            body="LanceDB provides the embedded vector index.",
        ),
    }
    for rel, text in pages.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


# --- related / paths: explicit bounds + truthful trace (AC1) ---


def test_related_trace_reports_scope_direction_and_bounds(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """AC1: --trace emits a truthful diagnostic of scope/direction/bounds."""
    rc = main(
        [
            "related",
            str(kb_root),
            "Lumio Overview",
            "--scope",
            "discovery",
            "--direction",
            "both",
            "--depth",
            "2",
            "--max-results",
            "5",
            "--trace",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "# trace:" in out
    assert "scope=discovery" in out
    assert "direction=both" in out
    assert "max_depth=2" in out
    assert "max_results=5" in out
    # The trace reports ONLY what the traversal used; it does not imply a
    # persisted artifact was the traversal source (ADR-0011 truthfulness).
    assert "artifact=" not in out


def test_paths_max_depth_bounds_traversal(graph_kb: Path):
    """AC1: --max-depth is an explicit bound. A 2-hop path is unreachable at depth 1."""
    # Canonical path Lumio Overview -> Architecture -> Technology Stack is 2 hops.
    rc_shallow = main(
        ["paths", str(graph_kb), "Lumio Overview", "Technology Stack", "--max-depth", "1"]
    )
    assert rc_shallow == 1  # bounded out — no path within 1 hop
    rc_deep = main(
        ["paths", str(graph_kb), "Lumio Overview", "Technology Stack", "--max-depth", "2"]
    )
    assert rc_deep == 0


def test_paths_trace_reports_not_found(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(
        [
            "paths",
            str(kb_root),
            "Lumio Overview",
            "Architecture",
            "--max-depth",
            "0",
            "--trace",
        ]
    )
    # depth 0 forbids any hop, so no path to a different title.
    assert rc == 1
    out = capsys.readouterr().out
    assert "found=false" in out


# --- Hot Index + Navigation Index ladder entry points (AC2) ---


# --- Graceful graph recovery (AC4) ---


def test_corrupt_artifact_does_not_block_zero_index_operation(
    graph_kb: Path, capsys: pytest.CaptureFixture[str]
):
    """AC4: a corrupt graph artifact never blocks search/page/related/paths."""
    index_dir = default_index_dir(graph_kb)
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / GRAPH_ARTIFACT_FILENAME).write_bytes(b"\x00\x01\x02 not msgpack")
    capsys.readouterr()  # clear
    # Every zero-index operation still works.
    assert main(["search", str(graph_kb), "LanceDB"]) == 0
    assert main(["page", str(graph_kb), "Architecture"]) == 0
    assert main(["related", str(graph_kb), "Lumio Overview"]) == 0
    assert main(["paths", str(graph_kb), "Lumio Overview", "Technology Stack"]) == 0
    # health reports the graph as not fresh (corrupt → ignored).
    rc = main(["health", str(graph_kb)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "graph_fresh:" in out and "False" in out


def test_health_reports_recovery_hint_when_graph_not_fresh(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    rc = main(["health", str(kb_root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "graph_recovery:" in out
    assert "--rebuild" in out


def test_health_rebuild_materializes_fresh_graph(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    index_dir = default_index_dir(kb_root)
    assert not (index_dir / GRAPH_ARTIFACT_FILENAME).exists()
    rc = main(["health", str(kb_root), "--rebuild"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "graph_materialized:" in out and "True" in out
    assert "graph_fresh:" in out and "True" in out
    assert (index_dir / GRAPH_ARTIFACT_FILENAME).is_file()


def test_health_rebuild_overwrites_corrupt_artifact(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    index_dir = default_index_dir(kb_root)
    index_dir.mkdir(parents=True, exist_ok=True)
    artifact = index_dir / GRAPH_ARTIFACT_FILENAME
    artifact.write_bytes(b"corrupt garbage")
    rc = main(["health", str(kb_root), "--rebuild"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "graph_fresh:" in out and "True" in out
    # Subsequent plain health (no rebuild) stays fresh, no recovery hint.
    capsys.readouterr()
    assert main(["health", str(kb_root)]) == 0
    out2 = capsys.readouterr().out
    assert "graph_fresh:" in out2 and "True" in out2
    assert "graph_recovery:" not in out2


# ---------------------------------------------------------------------------
# LUMIO_KB_PATH default (issue #125 follow-up)
# ---------------------------------------------------------------------------


def test_kb_path_missing_gives_actionable_error(tmp_path, monkeypatch, capsys):
    """Neither positional nor env var → actionable error, exit 2."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_KB_PATH", raising=False)
    rc = main(["search", "query"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LUMIO_KB_PATH" in err


# ---------------------------------------------------------------------------
# setup command
# ---------------------------------------------------------------------------


def test_setup_creates_new_kb_and_config(tmp_path, monkeypatch, capsys):
    """setup creates KB, .env, and AGENTS.md from scratch."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_KB_PATH", raising=False)
    kb_path = tmp_path / "my-wiki"

    rc = main(["setup", str(kb_path)])
    assert rc == 0

    # KB created
    assert (kb_path / "lumio.yaml").exists()
    assert (kb_path / ".lumio" / "ingest").is_dir()

    # .env written with absolute path
    env = (tmp_path / ".env").read_text()
    assert "LUMIO_KB_PATH=" in env
    assert str(kb_path.resolve()) in env

    # AGENTS.md written with the retrieval ladder
    agents_md = (tmp_path / "AGENTS.md").read_text()
    assert "## Lumio Knowledge Base" in agents_md
    assert "lumio-wiki search" in agents_md
    assert "retrieval ladder" in agents_md.lower()
    # ... and the Maintainer workflows (ADR-0015)
    assert "Maintenance (you are the Maintainer)" in agents_md
    assert "lumio-wiki lint" in agents_md
    assert "lumio-wiki cross-link" in agents_md
    assert "lumio-wiki dream" in agents_md


def test_setup_uses_existing_kb(tmp_path, monkeypatch, capsys):
    """setup does not overwrite an existing KB."""
    monkeypatch.chdir(tmp_path)
    kb_path = tmp_path / "existing"
    shutil.copytree(FIXTURES / "valid", kb_path)
    original_content = (kb_path / "technology.md").read_text()

    rc = main(["setup", str(kb_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already exists" in out

    # Content unchanged
    assert (kb_path / "technology.md").read_text() == original_content


def test_setup_no_agents_md_flag(tmp_path, monkeypatch):
    """--no-agents-md skips AGENTS.md writing."""
    monkeypatch.chdir(tmp_path)
    rc = main(["setup", str(tmp_path / "kb"), "--no-agents-md"])
    assert rc == 0
    assert not (tmp_path / "AGENTS.md").exists()


def test_setup_updates_existing_agents_md(tmp_path, monkeypatch):
    """setup replaces its section when AGENTS.md already has one."""
    monkeypatch.chdir(tmp_path)
    existing = "# My Project\n\nSome content.\n"
    (tmp_path / "AGENTS.md").write_text(existing)

    rc = main(["setup", str(tmp_path / "kb")])
    assert rc == 0

    content = (tmp_path / "AGENTS.md").read_text()
    assert "# My Project" in content  # original preserved
    assert "## Lumio Knowledge Base" in content  # section added


def test_setup_repeated_run_never_duplicates_the_section(tmp_path, monkeypatch):
    """Regression: repeated setup refreshes must not duplicate the KB section.

    The writer's replace span runs from the marker to the next `##` heading
    AFTER the section's own `## Lumio Knowledge Base` heading; terminating at
    the section's own heading prepended a fresh copy on every refresh.
    """
    monkeypatch.chdir(tmp_path)
    kb = tmp_path / "kb"
    for _ in range(3):
        assert main(["setup", str(kb)]) == 0
    content = (tmp_path / "AGENTS.md").read_text()
    assert content.count("## Lumio Knowledge Base") == 1
    assert content.count("<!-- lumio-wiki-kb -->") == 1


# ---------------------------------------------------------------------------
# Project .env loading (issue #152, ADR-0017)
#
# A fresh lumio-wiki subprocess must load LUMIO_KB_PATH from the project
# .env that `setup` wrote. The in-process tests below assert precedence
# (positional > exported env > .env > actionable error); the subprocess
# tests are the authoritative fresh-process proof the in-process
# monkeypatch.setenv simulation in the test above could not provide.
# ---------------------------------------------------------------------------


def _clean_subprocess_env() -> dict[str, str]:
    """A subprocess env without LUMIO_KB_PATH so only the project .env is seen."""
    return {key: value for key, value in os.environ.items() if key != "LUMIO_KB_PATH"}


def _run_cli_in_subprocess(
    args: list[str], cwd: Path, *, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a fresh process with optional environment overrides."""
    env = _clean_subprocess_env()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from lumio_wiki.cli import main; raise SystemExit(main())",
            *args,
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_subprocess_positional_path_overrides_env_and_env_file(tmp_path: Path):
    """Fresh process: positional path outranks exported env and project .env."""
    project = tmp_path / "project"
    project.mkdir()
    real_kb = project / "real-kb"
    shutil.copytree(FIXTURES / "valid", real_kb)
    (project / ".env").write_text(f"LUMIO_KB_PATH={project / 'file-kb'}\n", encoding="utf-8")
    result = _run_cli_in_subprocess(
        ["search", str(real_kb), "Technology"],
        cwd=project,
        env_overrides={"LUMIO_KB_PATH": str(project / "env-kb")},
    )
    assert result.returncode == 0, result.stderr
    assert "Technology Stack" in result.stdout


def test_subprocess_exported_env_overrides_env_file(tmp_path: Path):
    """Fresh process: exported LUMIO_KB_PATH outranks project .env."""
    project = tmp_path / "project"
    project.mkdir()
    real_kb = project / "real-kb"
    shutil.copytree(FIXTURES / "valid", real_kb)
    (project / ".env").write_text(f"LUMIO_KB_PATH={project / 'file-kb'}\n", encoding="utf-8")
    result = _run_cli_in_subprocess(
        ["search", "Technology"],
        cwd=project,
        env_overrides={"LUMIO_KB_PATH": str(real_kb)},
    )
    assert result.returncode == 0, result.stderr
    assert "Technology Stack" in result.stdout


def test_empty_exported_env_does_not_fall_back_to_env_file(tmp_path: Path, monkeypatch, capsys):
    """An explicitly empty process value remains authoritative over .env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LUMIO_KB_PATH", "")
    (tmp_path / ".env").write_text(f"LUMIO_KB_PATH={tmp_path / 'valid-kb'}\n", encoding="utf-8")

    rc = main(["search", "Technology"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no Knowledge Base path provided" in err
    assert ".env" in err


def test_missing_or_malformed_env_file_gives_actionable_error(tmp_path: Path, monkeypatch, capsys):
    """A malformed .env (no key=value) yields a single actionable error."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_KB_PATH", raising=False)
    # Not a KEY=value line, so LUMIO_KB_PATH is absent.
    (tmp_path / ".env").write_text("this line is malformed\n", encoding="utf-8")

    rc = main(["search", "query"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LUMIO_KB_PATH" in err
    assert ".env" in err


def test_subprocess_invalid_env_path_is_actionable(tmp_path: Path):
    """A path value with an embedded NUL must not produce a traceback."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_bytes(b"LUMIO_KB_PATH=/tmp/\x00bad\n")

    result = _run_cli_in_subprocess(["validate"], cwd=project)
    assert result.returncode == 2
    assert "no Knowledge Base path provided" in result.stderr
    assert "Traceback" not in result.stderr


def test_subprocess_setup_then_validate_reads_env(tmp_path: Path):
    """AC1: a fresh `lumio-wiki validate` reads LUMIO_KB_PATH from project .env.

    This is the authoritative fresh-subprocess reproduction of the bug report:
    setup writes .env, then a SEPARATE process validates with no positional
    path and no exported environment variable.
    """
    project = tmp_path / "project"
    project.mkdir()
    kb = project / "kb"
    shutil.copytree(FIXTURES / "valid", kb)

    setup = _run_cli_in_subprocess(["setup", str(kb)], cwd=project)
    assert setup.returncode == 0, setup.stderr

    validate = _run_cli_in_subprocess(["validate"], cwd=project)
    assert validate.returncode == 0, validate.stderr


def test_subprocess_journey_reads_env_for_path_commands(tmp_path: Path):
    """AC2: each path-bearing command resolves LUMIO_KB_PATH from .env.

    After setup, validate, search, page, proposal list, lint, and health all
    run with no positional <kb> and no exported variable in a fresh process.
    """
    project = tmp_path / "project"
    project.mkdir()
    shutil.copytree(FIXTURES / "valid", project / "kb")

    setup = _run_cli_in_subprocess(["setup", str(project / "kb")], cwd=project)
    assert setup.returncode == 0, setup.stderr

    journeys: list[tuple[list[str], str | None]] = [
        (["validate"], None),
        (["search", "Technology"], "Technology Stack"),
        (["page", "Technology Stack"], "Technology Stack"),
        (["proposal", "list"], None),
        (["lint"], None),
        (["health"], None),
    ]
    for command, expected in journeys:
        result = _run_cli_in_subprocess(command, cwd=project)
        assert result.returncode == 0, f"{command}: {result.stderr or result.stdout}"
        if expected is not None:
            assert expected in result.stdout, f"{command}: {result.stdout}"


def test_subprocess_env_file_does_not_leak_arbitrary_keys(tmp_path: Path):
    """AC6: reading .env never injects arbitrary keys into the process env."""
    project = tmp_path / "project"
    project.mkdir()
    shutil.copytree(FIXTURES / "valid", project / "kb")
    arbitrary = "LUMIO_TEST_LEAK_KEY_152"
    # Run setup, then append an arbitrary key to .env.
    setup = _run_cli_in_subprocess(["setup", str(project / "kb")], cwd=project)
    assert setup.returncode == 0, setup.stderr
    env_file = project / ".env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + f"{arbitrary}=secret\n",
        encoding="utf-8",
    )
    # A fresh process that resolves the KB must not expose the arbitrary key.
    # Run a raw Python probe (not the CLI) so only the loader runs.
    probe_script = (
        "import os, sys; "
        "from lumio_wiki.env_loader import discover_kb_path_from_project_env; "
        "discover_kb_path_from_project_env(); "
        f"sys.stdout.write('LEAKED' if os.environ.get({arbitrary!r}) else 'clean')"
    )
    probe = subprocess.run(
        [sys.executable, "-c", probe_script],
        cwd=str(project),
        env=_clean_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    assert "clean" in probe.stdout, probe.stdout


# ---------------------------------------------------------------------------
# source subcommands: private Knowledge Source lifecycle (issue #133)
# ---------------------------------------------------------------------------

# A Compiled Page backed by the explicit ``policy`` Knowledge Source identity.
# The private Source Registry holds the identity; this page's ``sources[].id``
# is the ONLY thing that produces a retirement impact for ``policy`` — a
# renamed file registered under ``policy`` cannot create support for any page
# that does not declare it.
SOURCE_POLICY_PAGE_MD = textwrap.dedent(
    """\
    ---
    title: "CLI Page"
    aliases: []
    tags:
      - "cli"
    summary: "A Compiled Page backed by the policy Knowledge Source."
    lifecycle: "approved"
    visibility: "internal"
    sources:
      - id: "policy"
        title: "Policy Knowledge Source"
    synthetic: false
    ---

    # CLI Page

    Backed by the policy Knowledge Source for lifecycle tests.
    """
)


@pytest.fixture
def source_kb(tmp_path: Path) -> Path:
    """A Knowledge Base whose ``CLI Page`` declares the ``policy`` source id."""
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", root)
    (root / "cli_page.md").write_text(SOURCE_POLICY_PAGE_MD, encoding="utf-8")
    return root


def _extract_proposal_id(output: str) -> str:
    """Pull the staged proposal id from a ``Staged proposal <id>`` line."""
    return output.split("Staged proposal")[1].split()[0]


def _register_policy(kb: Path, source_file: Path) -> int:
    """Register the ``policy`` Knowledge Source and return the CLI exit code."""
    return main(
        ["source", "register", str(kb), "--source-id", "policy", "--file", str(source_file)]
    )


def test_source_retire_requires_explicit_registered_source_id(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["source", "retire", str(kb_root), "--source-id", "unknown"])
    assert rc == 1
    assert "unknown Knowledge Source" in capsys.readouterr().err


def test_source_retire_stages_safe_page_impact_output(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # ``source_kb`` contains ``sources: [{id: policy, ...}]`` for CLI Page.
    assert _register_policy(source_kb, source_file) == 0
    assert main(["source", "retire", str(source_kb), "--source-id", "policy"]) == 0
    output = capsys.readouterr().out
    assert "Staged proposal" in output
    assert "source_id:      policy" in output
    assert "sole-source-lost: CLI Page" in output
    assert str(source_file) not in output


def test_source_register_requires_explicit_source_id_and_file(source_kb: Path) -> None:
    # ``--source-id`` and ``--file`` are both required by the parser.
    with pytest.raises(SystemExit):
        main(["source", "register", str(source_kb), "--source-id", "policy"])
    with pytest.raises(SystemExit):
        main(["source", "register", str(source_kb), "--file", "/tmp/unused.md"])


def test_source_register_then_list_shows_active_identity(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _register_policy(source_kb, source_file) == 0
    capsys.readouterr()  # drain register output
    assert main(["source", "list", str(source_kb)]) == 0
    out = capsys.readouterr().out
    assert "policy" in out
    assert "active" in out
    # The listing never discloses raw bytes or the local source path.
    assert str(source_file) not in out


def test_source_candidate_leaves_source_active_without_staging(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    rc = main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            "object store unavailable",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "policy" in out
    assert "object store unavailable" in out
    assert "pending" in out
    # Candidate-only behavior: the source stays active and no proposal stages.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get("policy").status == "active"
    assert store.list() == []


def test_source_dismiss_candidate_records_decision_without_staging(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            "object store unavailable",
        ]
    )
    candidate_out = capsys.readouterr().out
    candidate_id = candidate_out.split("candidate_id:")[1].split()[0]
    rc = main(
        [
            "source",
            "dismiss-candidate",
            str(source_kb),
            "--candidate-id",
            candidate_id,
            "--source-id",
            "policy",
        ]
    )
    assert rc == 0
    dismiss_out = capsys.readouterr().out
    assert "dismissed" in dismiss_out
    # Dismiss records a decision without staging any proposal.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "dismissed"
    assert store.list() == []


def test_source_dismiss_candidate_requires_explicit_source_id(
    source_kb: Path, source_file: Path
) -> None:
    # ``--source-id`` is required by the parser because dismissal mutates state.
    _register_policy(source_kb, source_file)
    with pytest.raises(SystemExit):
        main(
            [
                "source",
                "dismiss-candidate",
                str(source_kb),
                "--candidate-id",
                "deadbeef",
            ]
        )


def test_source_dismiss_candidate_refuses_mismatched_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            "object store unavailable",
        ]
    )
    candidate_out = capsys.readouterr().out
    candidate_id = candidate_out.split("candidate_id:")[1].split()[0]

    rc = main(
        [
            "source",
            "dismiss-candidate",
            str(source_kb),
            "--candidate-id",
            candidate_id,
            "--source-id",
            "wrong",
        ]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "does not belong" in err
    # A mismatch must be safe: the candidate stays pending and nothing stages.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"
    assert store.list() == []


# --- #133 final review: secret-bearing invalid source/candidate ids at the CLI ---
#
# A crafted CLI request with a secret-bearing source/candidate id must fail
# with a GENERIC error: the secret, path, and hash never reach stdout/stderr,
# and no state mutates (no proposal staged, candidate/source unchanged).

_SECRET_SOURCE_ID = "sk-leaked-api-key;token=abc123"
_SECRET_CANDIDATE_ID = "/var/lib/lumio/secret.key"


def test_source_retire_rejects_secret_bearing_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    rc = main(["source", "retire", str(source_kb), "--source-id", _SECRET_SOURCE_ID])
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_SOURCE_ID not in (combined.out + combined.err)
    assert "secret.key" not in combined.err
    # No mutation: no proposal staged.
    assert lw.IngestStore(source_kb / ".lumio" / "ingest").list() == []


def test_source_reactivate_rejects_secret_bearing_source_id(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    replacement = tmp_path / "replacement.md"
    replacement.write_text("# replacement\n", encoding="utf-8")
    rc = main(
        [
            "source",
            "reactivate",
            str(source_kb),
            "--source-id",
            _SECRET_SOURCE_ID,
            "--file",
            str(replacement),
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_SOURCE_ID not in (combined.out + combined.err)
    assert lw.IngestStore(source_kb / ".lumio" / "ingest").list() == []


def test_source_dismiss_rejects_secret_bearing_candidate_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()
    candidate_id = _record_candidate_cli(source_kb, capsys)
    rc = main(
        [
            "source",
            "dismiss-candidate",
            str(source_kb),
            "--candidate-id",
            _SECRET_CANDIDATE_ID,
            "--source-id",
            "policy",
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_CANDIDATE_ID not in (combined.out + combined.err)
    # No mutation: the real candidate stays pending.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"


def test_source_confirm_rejects_secret_bearing_candidate_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()
    candidate_id = _record_candidate_cli(source_kb, capsys)
    rc = main(
        [
            "source",
            "confirm-candidate",
            str(source_kb),
            "--candidate-id",
            _SECRET_CANDIDATE_ID,
            "--source-id",
            "policy",
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_CANDIDATE_ID not in (combined.out + combined.err)
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"
    assert store.list() == []


def test_source_dismiss_rejects_secret_bearing_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()
    candidate_id = _record_candidate_cli(source_kb, capsys)
    rc = main(
        [
            "source",
            "dismiss-candidate",
            str(source_kb),
            "--candidate-id",
            candidate_id,
            "--source-id",
            _SECRET_SOURCE_ID,
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_SOURCE_ID not in (combined.out + combined.err)
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"


def test_source_confirm_rejects_secret_bearing_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()
    candidate_id = _record_candidate_cli(source_kb, capsys)
    rc = main(
        [
            "source",
            "confirm-candidate",
            str(source_kb),
            "--candidate-id",
            candidate_id,
            "--source-id",
            _SECRET_SOURCE_ID,
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_SOURCE_ID not in (combined.out + combined.err)
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"
    assert store.list() == []


def _record_candidate_cli(source_kb: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Record a ``policy`` retirement candidate via the CLI; return its id."""
    main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            "object store unavailable",
        ]
    )
    candidate_out = capsys.readouterr().out
    return candidate_out.split("candidate_id:")[1].split()[0]


def test_source_retire_impacts_only_pages_declaring_the_source_id(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A renamed copy of the fixture source, registered under ``policy``, cannot
    # create support: only a Compiled Page whose ``sources[].id: policy`` is
    # impacted. The renamed filename and its bytes (which declare a different
    # source id) never influence page-level support.
    renamed = tmp_path / "renamed-policy-source.md"
    shutil.copyfile(source_file, renamed)
    assert (
        main(
            [
                "source",
                "register",
                str(source_kb),
                "--source-id",
                "policy",
                "--file",
                str(renamed),
            ]
        )
        == 0
    )
    capsys.readouterr()  # drain register output
    assert main(["source", "retire", str(source_kb), "--source-id", "policy"]) == 0
    out = capsys.readouterr().out
    assert "sole-source-lost: CLI Page" in out
    # The other fixture pages declare different source ids — never impacted.
    assert "Lumio Overview" not in out
    assert "Architecture" not in out
    assert "Technology Stack" not in out
    # The renamed file path never leaks into the output.
    assert str(renamed) not in out


def test_source_reactivate_stages_new_version_under_existing_id(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    assert main(["source", "retire", str(source_kb), "--source-id", "policy"]) == 0
    retire_id = _extract_proposal_id(capsys.readouterr().out)
    assert main(["publish", str(source_kb), retire_id]) == 0
    capsys.readouterr()  # drain publish output
    assert (
        lw.IngestStore(source_kb / ".lumio" / "ingest").source_registry.get("policy").status
        == "retired"
    )

    replacement = tmp_path / "replacement.md"
    replacement.write_text("# replacement policy bytes\n", encoding="utf-8")
    rc = main(
        [
            "source",
            "reactivate",
            str(source_kb),
            "--source-id",
            "policy",
            "--file",
            str(replacement),
        ]
    )
    assert rc == 0
    reactivate_out = capsys.readouterr().out
    assert "Staged proposal" in reactivate_out
    assert "source_id:      policy" in reactivate_out
    # Safe output: the replacement path and bytes never appear.
    assert str(replacement) not in reactivate_out
    reactivate_id = _extract_proposal_id(reactivate_out)

    # The source stays retired until the reactivation proposal publishes.
    assert (
        lw.IngestStore(source_kb / ".lumio" / "ingest").source_registry.get("policy").status
        == "retired"
    )
    assert main(["publish", str(source_kb), reactivate_id]) == 0
    active = lw.IngestStore(source_kb / ".lumio" / "ingest").source_registry.get("policy")
    assert active.status == "active"
    assert len(active.versions) == 2
    assert active.versions[0].source_id == active.versions[1].source_id == "policy"
    assert active.versions[0].content_hash != active.versions[1].content_hash


def test_source_reactivation_impact_counts_reactivated_source_as_support(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #133 final review: reactivation evaluates support AFTER including the
    # reactivated source, so the sole-source ``CLI Page`` is still-supported
    # (not sole-source-lost, which is the retirement outcome for the same page).
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    assert main(["source", "retire", str(source_kb), "--source-id", "policy"]) == 0
    retire_id = _extract_proposal_id(capsys.readouterr().out)
    assert main(["publish", str(source_kb), retire_id]) == 0
    capsys.readouterr()  # drain publish output

    replacement = tmp_path / "replacement.md"
    replacement.write_text("# replacement policy bytes\n", encoding="utf-8")
    rc = main(
        [
            "source",
            "reactivate",
            str(source_kb),
            "--source-id",
            "policy",
            "--file",
            str(replacement),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Action-aware: the reactivated source itself restores support, so the
    # sole-source CLI Page renders still-supported.
    assert "still-supported: CLI Page" in out
    assert "sole-source-lost" not in out


# ---------------------------------------------------------------------------
# source file reads: safe errors with no path or traceback leakage
# ---------------------------------------------------------------------------


def test_source_register_missing_file_reports_generic_error_without_path(
    source_kb: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A missing source file must produce a generic, path-free user-facing error.
    missing = tmp_path / "does-not-exist.md"
    rc = main(
        ["source", "register", str(source_kb), "--source-id", "policy", "--file", str(missing)]
    )
    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # The supplied path (and its filename) never reaches the user, and no
    # traceback is leaked.
    assert str(missing) not in combined
    assert "does-not-exist.md" not in combined
    assert "Traceback" not in combined


def test_source_register_unreadable_file_reports_generic_error_without_path(
    source_kb: Path,
    source_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Existence/read race: the file exists but reading it raises OSError. The
    # raw OSError text (which may carry a path or credentials) must never reach
    # the user, and no traceback may leak.
    real_read_bytes = Path.read_bytes

    def raising_read_bytes(self: Path) -> bytes:
        if self == source_file:
            raise OSError("disk read failure at /secret/credentials.key")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", raising_read_bytes)

    rc = _register_policy(source_kb, source_file)

    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert str(source_file) not in combined
    assert "/secret/credentials.key" not in combined
    assert "disk read failure" not in combined
    assert "Traceback" not in combined


def test_source_register_missing_registered_source_reports_generic_error(
    source_kb: Path,
    source_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Impossible-state guard: if the just-registered source is absent from the
    # public listing (an invariant the static type cannot prove is non-None),
    # the CLI must fail with a generic, path/secret-free error (rc=1) and never
    # leak a traceback from an unguarded ``None`` dereference.
    from lumio_wiki.proposal_pipeline import ProposalPipeline

    monkeypatch.setattr(ProposalPipeline, "list_sources", lambda self: [])

    rc = _register_policy(source_kb, source_file)

    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" not in combined
    # The local source path (and any byte content) is never disclosed.
    assert str(source_file) not in combined


def test_source_reactivate_missing_file_reports_generic_error_without_path(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # ``--file`` is read through the same safe helper for reactivate.
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    retire_rc = main(["source", "retire", str(source_kb), "--source-id", "policy"])
    retire_id = _extract_proposal_id(capsys.readouterr().out)
    assert retire_rc == 0
    assert main(["publish", str(source_kb), retire_id]) == 0
    capsys.readouterr()  # drain publish output

    missing = tmp_path / "absent-replacement.md"
    rc = main(
        [
            "source",
            "reactivate",
            str(source_kb),
            "--source-id",
            "policy",
            "--file",
            str(missing),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert str(missing) not in combined
    assert "absent-replacement.md" not in combined
    assert "Traceback" not in combined


def test_source_candidate_rejects_trigger_outside_vocabulary_safely(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #133: a secret-bearing or arbitrary --trigger is rejected safely. The CLI
    # never echoes the value into its output and never persists a candidate.
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    secret = "password=hunter2;token=abc123"
    rc = main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            secret,
        ]
    )
    assert rc != 0
    captured = capsys.readouterr()
    # The secret-bearing trigger is never echoed into stdout or stderr.
    assert secret not in captured.out
    assert secret not in captured.err
    # No candidate was persisted.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.list_candidates() == []


def test_source_candidate_help_lists_controlled_trigger_choices(
    source_kb: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The --trigger help documents the exact controlled vocabulary so a
    # Maintainer knows the accepted signals without trial and error.
    with pytest.raises(SystemExit):
        main(["source", "candidate", str(source_kb), "--help"])
    out = capsys.readouterr().out
    # argparse wraps long help across lines; normalize whitespace (a display
    # detail) before asserting each controlled signal is documented.
    import re

    flat = re.sub(r"\s+", " ", out)
    for trigger in lw.RETIREMENT_CANDIDATE_TRIGGERS:
        assert trigger in flat


# ---------------------------------------------------------------------------
# source confirm-candidate: staged retirement through review (#133 final fix)
# ---------------------------------------------------------------------------


def _record_candidate(kb: Path, trigger: str = "watched file missing") -> str:
    """Record a retirement candidate for ``policy`` and return its id."""
    main(
        [
            "source",
            "candidate",
            str(kb),
            "--source-id",
            "policy",
            "--trigger",
            trigger,
        ]
    )
    out = _capture_candidate_output(kb)
    return out.split("candidate_id:")[1].split()[0]


def _capture_candidate_output(kb: Path) -> str:
    """Read the pending candidate id from the private registry (test only)."""
    import json

    registry = kb / ".lumio" / "ingest" / "source-registry" / "sources.json"
    state = json.loads(registry.read_text())
    for candidate in state.get("candidates", []):
        if candidate["source_id"] == "policy":
            return f"candidate_id: {candidate['id']}"
    raise AssertionError("no candidate recorded for policy")


def test_source_confirm_candidate_stages_retirement_proposal(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    candidate_id = _record_candidate(source_kb)
    capsys.readouterr()  # drain candidate output

    rc = main(
        [
            "source",
            "confirm-candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--candidate-id",
            candidate_id,
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out
    assert "source_id:      policy" in out
    # The candidate is marked confirmed and the source stays active until publish.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "confirmed"
    assert store.source_registry.get("policy").status == "active"


def test_source_confirm_candidate_requires_explicit_ids(source_kb: Path) -> None:
    # Both --source-id and --candidate-id are required by the parser.
    with pytest.raises(SystemExit):
        main(["source", "confirm-candidate", str(source_kb), "--source-id", "policy"])
    with pytest.raises(SystemExit):
        main(["source", "confirm-candidate", str(source_kb), "--candidate-id", "deadbeef"])


def test_source_confirm_candidate_refuses_mismatched_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    candidate_id = _record_candidate(source_kb)
    capsys.readouterr()  # drain candidate output

    rc = main(
        [
            "source",
            "confirm-candidate",
            str(source_kb),
            "--source-id",
            "wrong",
            "--candidate-id",
            candidate_id,
        ]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "does not belong" in err
    # A mismatch is safe: the candidate stays pending and no proposal stages.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"
    assert store.source_registry.get("policy").status == "active"
    assert store.list() == []


def test_source_confirm_candidate_leaves_source_active_until_publish(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    candidate_id = _record_candidate(source_kb)
    capsys.readouterr()  # drain candidate output
    assert (
        main(
            [
                "source",
                "confirm-candidate",
                str(source_kb),
                "--source-id",
                "policy",
                "--candidate-id",
                candidate_id,
            ]
        )
        == 0
    )
    confirm_out = capsys.readouterr().out
    proposal_id = _extract_proposal_id(confirm_out)

    # The source is still active before the staged proposal publishes.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get("policy").status == "active"
    assert main(["publish", str(source_kb), proposal_id]) == 0
    # Re-read from disk (each CLI command builds its own store instance, so the
    # in-memory state above is stale after the publish wrote the file).
    published_store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert published_store.source_registry.get("policy").status == "retired"


# ---------------------------------------------------------------------------
# register boundary: no duplicate creation, safe label contract (#133 final fix)
# ---------------------------------------------------------------------------


def test_source_duplicate_register_is_refused_without_mutation(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A second registration of an existing active id is refused generically and
    # never appends a version (#133 final review).
    assert _register_policy(source_kb, source_file) == 0
    capsys.readouterr()  # drain first register
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    versions_before = len(store.source_registry.get("policy").versions)

    rc = main(
        ["source", "register", str(source_kb), "--source-id", "policy", "--file", str(source_file)]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "replacement is not available" in err
    # No mutation: the version count is unchanged and no second version appears.
    source = store.source_registry.get("policy")
    assert len(source.versions) == versions_before
    assert source.status == "active"


def test_source_register_rejects_invalid_source_id_safely(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A malformed source id is rejected at the boundary; the rejected value
    # is never echoed into the CLI output and nothing is persisted.
    invalid_id = "invalid source id 152"
    rc = main(
        [
            "source",
            "register",
            str(source_kb),
            "--source-id",
            invalid_id,
            "--file",
            str(source_file),
        ]
    )
    assert rc != 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert invalid_id not in combined
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert all(s.source_id != invalid_id for s in store.source_registry.list())


# ---------------------------------------------------------------------------
# Authorized Source Artifact inspection: inspect / fetch / link (issue #165).
# ---------------------------------------------------------------------------

INSPECTION_RAW = b"%PDF-1.4 the exact original policy artifact (#165)"


def _retained_policy_source(
    kb: Path, store_root: Path, *, filename: str | None = "policy.pdf"
) -> lw.LocalDirectoryArtifactStore:
    """Register + retain the ``policy`` source through the public seams."""
    ingest = lw.IngestStore(kb / ".lumio" / "ingest")
    ingest.source_registry.register_source(
        "policy",
        INSPECTION_RAW,
        filename=filename,
        content_type="application/pdf",
    )
    store = lw.LocalDirectoryArtifactStore(store_root)
    lw.retain_artifact(
        store,
        ingest.source_registry,
        source_id="policy",
        raw_bytes=INSPECTION_RAW,
        content_type="application/pdf",
        filename=filename,
    )
    return store


def test_source_inspect_registry_mode_reports_verified_artifact(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "artifact-store"
    _retained_policy_source(source_kb, store_root)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))

    assert main(["source", "inspect", str(source_kb), "--source-id", "policy"]) == 0
    out = capsys.readouterr().out
    assert "source_id:       policy" in out
    assert f"content_hash:    {lw.artifact_content_hash(INSPECTION_RAW)[:12]}" in out
    assert "filename:        policy.pdf" in out
    assert "media_type:      application/pdf" in out
    assert f"size:            {len(INSPECTION_RAW)}" in out
    assert "bound_to:        current registry version (local worktree)" in out
    assert "availability:    retained (digest and size verified)" in out
    assert "authorization:   granted (private Source Artifact Store read verified)" in out
    # Private store layout and raw bytes never appear in ordinary output.
    assert str(store_root) not in out
    assert "artifacts" not in out


def test_source_inspect_without_store_reports_not_retained(
    source_kb: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ingest = lw.IngestStore(source_kb / ".lumio" / "ingest")
    ingest.source_registry.register_source("policy", INSPECTION_RAW, filename="p.pdf")
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)

    assert main(["source", "inspect", str(source_kb), "--source-id", "policy"]) == 0
    out = capsys.readouterr().out
    assert "availability:    not retained (no Source Artifact Store configured)" in out
    assert "authorization:   granted (private registry view)" in out


def test_source_inspect_unknown_source_is_absent_binding(
    source_kb: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    assert main(["source", "inspect", str(source_kb), "--source-id", "ghost"]) == 1
    assert "unknown Knowledge Source" in capsys.readouterr().err


def test_source_inspect_published_version_resolves_manifest_binding(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "artifact-store"
    store = _retained_policy_source(source_kb, store_root)
    lw.write_binding_manifest(
        store,
        lw.SourceBindingManifest(
            published_version="v2026",
            fingerprint="fp",
            created_at="2026-08-21T00:00:00Z",
            entries=[
                lw.SourceBindingEntry(
                    page_title="CLI Page",
                    source_id="policy",
                    content_hash=lw.artifact_content_hash(INSPECTION_RAW),
                    content_type="application/pdf",
                    filename="policy.pdf",
                    size=len(INSPECTION_RAW),
                )
            ],
        ),
    )
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))

    assert (
        main(
            [
                "source",
                "inspect",
                str(source_kb),
                "--source-id",
                "policy",
                "--published-version",
                "v2026",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "bound_to:        published version v2026 (Source Binding Manifest)" in out


def test_source_inspect_published_version_without_store_fails_closed(
    source_kb: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    rc = main(
        [
            "source",
            "inspect",
            str(source_kb),
            "--source-id",
            "policy",
            "--published-version",
            "v2026",
        ]
    )
    assert rc == 1
    assert "no private Source Artifact Store is configured" in capsys.readouterr().err


def test_source_inspect_missing_manifest_is_historical_mismatch(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "artifact-store"
    _retained_policy_source(source_kb, store_root)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))

    rc = main(
        [
            "source",
            "inspect",
            str(source_kb),
            "--source-id",
            "policy",
            "--published-version",
            "v-does-not-exist",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no Source Binding Manifest" in err
    # Never substitutes the registry's current version silently.
    assert "refusing to substitute" in err


def test_source_fetch_writes_byte_exact_original(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "artifact-store"
    _retained_policy_source(source_kb, store_root)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    out_dir = tmp_path / "fetched"
    out_dir.mkdir()
    out_file = out_dir / "exact.pdf"

    assert (
        main(
            ["source", "fetch", str(source_kb), "--source-id", "policy", "--output", str(out_file)]
        )
        == 0
    )
    assert out_file.read_bytes() == INSPECTION_RAW
    assert "digest and size verified" in capsys.readouterr().out


def test_source_fetch_directory_output_receives_safe_filename(
    source_kb: Path, tmp_path: Path, monkeypatch
) -> None:
    store_root = tmp_path / "artifact-store"
    _retained_policy_source(source_kb, store_root, filename="../../policy.pdf")
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()

    assert (
        main(["source", "fetch", str(source_kb), "--source-id", "policy", "--output", str(out_dir)])
        == 0
    )
    fetched = out_dir / "policy.pdf"
    assert fetched.read_bytes() == INSPECTION_RAW


def test_source_fetch_rejects_corruption(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "artifact-store"
    _retained_policy_source(source_kb, store_root)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    # Tamper with the retained bytes behind the store's back.
    digest = lw.artifact_content_hash(INSPECTION_RAW)
    victim = store_root / "artifacts" / "policy" / digest
    victim.write_bytes(INSPECTION_RAW + b"tampered")
    out_file = tmp_path / "out.pdf"

    rc = main(
        ["source", "fetch", str(source_kb), "--source-id", "policy", "--output", str(out_file)]
    )
    assert rc == 1
    assert "failed digest verification" in capsys.readouterr().err
    assert not out_file.exists()


def test_source_fetch_without_store_fails_closed(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ingest = lw.IngestStore(source_kb / ".lumio" / "ingest")
    ingest.source_registry.register_source("policy", INSPECTION_RAW, filename="policy.pdf")
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    rc = main(
        [
            "source",
            "fetch",
            str(source_kb),
            "--source-id",
            "policy",
            "--output",
            str(tmp_path / "out.pdf"),
        ]
    )
    assert rc == 1
    assert "no private Source Artifact Store is configured" in capsys.readouterr().err


def test_source_fetch_unavailable_artifact_is_distinct_outcome(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "artifact-store"
    store = _retained_policy_source(source_kb, store_root)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    store.delete_artifact(source_id="policy", content_hash=lw.artifact_content_hash(INSPECTION_RAW))
    rc = main(
        [
            "source",
            "fetch",
            str(source_kb),
            "--source-id",
            "policy",
            "--output",
            str(tmp_path / "out.pdf"),
        ]
    )
    assert rc == 1
    assert "artifact not retained" in capsys.readouterr().err


def test_source_link_local_store_reports_signing_unsupported(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "artifact-store"
    _retained_policy_source(source_kb, store_root)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))

    rc = main(["source", "link", str(source_kb), "--source-id", "policy"])
    assert rc == 1
    assert "does not support signed URLs" in capsys.readouterr().err


def test_source_link_rejects_expires_over_one_hour(
    source_kb: Path, tmp_path: Path, monkeypatch
) -> None:
    store_root = tmp_path / "artifact-store"
    _retained_policy_source(source_kb, store_root)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))

    # A malformed/out-of-range --expires is a user-facing usage error: the
    # CliError default exit code 2 (like argparse), never a traceback.
    assert main(["source", "link", str(source_kb), "--source-id", "policy", "--expires", "2h"]) == 2


class _SigningInMemoryStore(lw.InMemoryArtifactStore):
    """A signing-capable stand-in so the CLI link path is testable offline."""

    def supports_signing(self) -> bool:  # pragma: no cover - trivial
        return True

    def signed_get_url(self, *, source_id: str, content_hash: str, expires_in) -> str:
        seconds = int(expires_in.total_seconds())
        return (
            f"https://objects.example/artifacts/{source_id}/{content_hash}"
            f"?method=GET&expires={seconds}"
        )


def test_source_link_signing_store_prints_url_with_secret_handling_note(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lumio_wiki import cli

    store = _SigningInMemoryStore()
    ingest = lw.IngestStore(source_kb / ".lumio" / "ingest")
    ingest.source_registry.register_source(
        "policy", INSPECTION_RAW, filename="policy.pdf", content_type="application/pdf"
    )
    lw.retain_artifact(
        store,
        ingest.source_registry,
        source_id="policy",
        raw_bytes=INSPECTION_RAW,
        content_type="application/pdf",
        filename="policy.pdf",
    )
    monkeypatch.setattr(cli, "_artifact_store_from_env", lambda: store)

    assert main(["source", "link", str(source_kb), "--source-id", "policy", "--expires", "1h"]) == 0
    out = capsys.readouterr().out
    assert "https://objects.example/artifacts/policy/" in out
    assert "expires:         1h (3600s)" in out
    assert "bearer secret" in out


def test_source_inspect_rejects_secret_bearing_source_id_without_echo(
    source_kb: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    secret_id = "sk-live-abc123"
    rc = main(["source", "inspect", str(source_kb), "--source-id", secret_id])
    assert rc == 1
    combined = capsys.readouterr()
    assert secret_id not in combined.out + combined.err


def test_source_inspect_rejects_unsafe_published_version_without_echo(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "artifact-store"
    _retained_policy_source(source_kb, store_root)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    for unsafe in ("../secret", "v1/../../etc", "token=abc", "a b"):
        rc = main(
            [
                "source",
                "inspect",
                str(source_kb),
                "--source-id",
                "policy",
                "--published-version",
                unsafe,
            ]
        )
        assert rc == 2, unsafe
        combined = capsys.readouterr()
        assert unsafe not in combined.out + combined.err


def test_source_s3_uri_resolves_active_published_version_once(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lumio_wiki import cli
    from lumio_wiki.s3_publish import PointerObservation

    store_root = tmp_path / "artifact-store"
    store = _retained_policy_source(source_kb, store_root)
    lw.write_binding_manifest(
        store,
        lw.SourceBindingManifest(
            published_version="v-active",
            fingerprint="fp",
            created_at="2026-08-21T00:00:00Z",
            entries=[
                lw.SourceBindingEntry(
                    page_title="CLI Page",
                    source_id="policy",
                    content_hash=lw.artifact_content_hash(INSPECTION_RAW),
                )
            ],
        ),
    )
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    monkeypatch.setattr(cli, "_build_publish_store", lambda uri: (object(), "kb-prefix"))
    monkeypatch.setattr(
        "lumio_wiki.s3_publish.observe_current_pointer",
        lambda store_arg, prefix: PointerObservation(version="v-active", e_tag="e"),
    )

    assert main(["source", "inspect", "s3://bucket/kb", "--source-id", "policy"]) == 0
    out = capsys.readouterr().out
    assert "bound_to:        published version v-active (Source Binding Manifest)" in out


def test_source_s3_uri_without_active_version_fails(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lumio_wiki import cli
    from lumio_wiki.s3_publish import PointerObservation

    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    monkeypatch.setattr(cli, "_build_publish_store", lambda uri: (object(), "kb-prefix"))
    monkeypatch.setattr(
        "lumio_wiki.s3_publish.observe_current_pointer",
        lambda store_arg, prefix: PointerObservation(version=None, e_tag=None),
    )

    assert main(["source", "inspect", "s3://bucket/kb", "--source-id", "policy"]) == 1
    assert "no active Published Version" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Source identity resolution (issue #176): `source resolve` maps a Source ID,
# Entity ID, Canonical Page Title, alias, or page path to ONE registered
# Knowledge Source; unknown input carries bounded close ids or the exact
# discovery command; ambiguity is truthful — never a guess.
# ---------------------------------------------------------------------------


DUAL_SOURCE_PAGE_MD = textwrap.dedent(
    """\
    ---
    title: "Dual Source Page"
    aliases: []
    tags: []
    summary: "A page backed by two Knowledge Sources."
    lifecycle: "approved"
    visibility: "internal"
    sources:
      - id: "policy"
        title: "Policy Knowledge Source"
      - id: "atlas-heatworks-product-catalog"
        title: "Atlas Catalog Knowledge Source"
    synthetic: false
    ---

    # Dual Source Page

    Backed by two Knowledge Sources for resolution tests.
    """
)


def test_source_resolve_by_page_title_reports_identity_and_availability(
    source_kb: Path, source_file: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _register_policy(source_kb, source_file) == 0
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)

    assert main(["source", "resolve", str(source_kb), "CLI Page"]) == 0
    out = capsys.readouterr().out
    assert "source_id:       policy" in out
    assert "matched_by:      canonical-title" in out
    assert "page:            CLI Page (cli_page.md)" in out
    assert "availability:    not retained (no Source Artifact Store configured)" in out
    assert "next:            lumio-wiki source inspect --source-id policy" in out


def test_source_resolve_by_exact_source_id_emits_json(
    source_kb: Path, source_file: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _register_policy(source_kb, source_file) == 0
    capsys.readouterr()  # drain register output
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)

    assert main(["source", "resolve", str(source_kb), "policy", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "resolved"
    assert payload["source_id"] == "policy"
    assert payload["matched_by"] == "source-id"
    assert payload["availability"] == "not retained (no Source Artifact Store configured)"
    # A resolved reference never carries a signed URL (that is link only).
    assert "http" not in json.dumps(payload)


def test_source_resolve_alias_and_path_surfaces(
    source_kb: Path, source_file: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _register_policy(source_kb, source_file) == 0
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)

    assert main(["source", "resolve", str(source_kb), "cli_page.md"]) == 0
    assert "matched_by:      path" in capsys.readouterr().out


def test_source_resolve_unknown_query_points_to_close_ids_or_command(
    source_kb: Path, source_file: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _register_policy(source_kb, source_file) == 0
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)

    # A title-shaped query for an unregistered source: the bounded close-id
    # set IS the pointer (issue #176: ids OR one command — never both, never
    # the raw query back).
    rc = main(["source", "resolve", str(source_kb), "Policy Knowledge SourceX"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "close Knowledge Source ids: policy" in err
    assert "Policy Knowledge SourceX" not in err

    # A query with nothing close points at exactly ONE discovery command.
    rc = main(["source", "resolve", str(source_kb), "something-unrelated"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no Knowledge Source resolved" in err
    assert "source list" in err
    assert "source resolve" not in err


def test_source_resolve_ambiguous_page_lists_bounded_candidates(
    source_kb: Path, source_file: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _register_policy(source_kb, source_file) == 0
    ingest = lw.IngestStore(source_kb / ".lumio" / "ingest")
    ingest.source_registry.register_source(
        "atlas-heatworks-product-catalog", b"atlas bytes", filename="atlas.pdf"
    )
    (source_kb / "dual_page.md").write_text(DUAL_SOURCE_PAGE_MD, encoding="utf-8")
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)

    assert main(["source", "resolve", str(source_kb), "Dual Source Page"]) == 1
    err = capsys.readouterr().err
    assert "ambiguous Source reference" in err
    assert "policy (Dual Source Page, dual_page.md)" in err
    assert "atlas-heatworks-product-catalog (Dual Source Page, dual_page.md)" in err

    # --json emits machine-selectable candidates on stdout with exit 1.
    assert main(["source", "resolve", str(source_kb), "Dual Source Page", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "ambiguous"
    assert {candidate["source_id"] for candidate in payload["candidates"]} == {
        "policy",
        "atlas-heatworks-product-catalog",
    }


def test_source_resolve_title_shaped_query_without_page_yields_bounded_hint(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reader-trial AC (#176): a failed lookup points at the exact id."""
    ingest = lw.IngestStore(source_kb / ".lumio" / "ingest")
    ingest.source_registry.register_source(
        "atlas-heatworks-product-catalog", b"atlas bytes", filename="atlas.pdf"
    )
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)

    # No page carries this title: resolution is by exact surfaces only (no
    # fuzzy id matching), but the unknown outcome's bounded hint names the
    # exact registered id (the reader-trial contract).
    rc = main(["source", "resolve", str(source_kb), "Atlas Heatworks Product Catalog"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "close Knowledge Source ids: atlas-heatworks-product-catalog" in err


def test_source_resolve_published_version_resolves_manifest_binding(
    source_kb: Path, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "artifact-store"
    store = _retained_policy_source(source_kb, store_root)
    lw.write_binding_manifest(
        store,
        lw.SourceBindingManifest(
            published_version="v2026",
            fingerprint="fp",
            created_at="2026-08-21T00:00:00Z",
            entries=[
                lw.SourceBindingEntry(
                    page_title="CLI Page",
                    source_id="policy",
                    content_hash=lw.artifact_content_hash(INSPECTION_RAW),
                    content_type="application/pdf",
                    filename="policy.pdf",
                    size=len(INSPECTION_RAW),
                )
            ],
        ),
    )
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))

    assert (
        main(
            [
                "source",
                "resolve",
                str(source_kb),
                "CLI Page",
                "--published-version",
                "v2026",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "source_id:       policy" in out
    assert "bound_to:        published version v2026 (Source Binding Manifest)" in out
    assert "availability:    retained (digest and size verified)" in out


def test_source_inspect_unknown_id_error_names_close_id_and_command(
    source_kb: Path, source_file: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #176 AC: the failed lookup error points at the exact id or command."""
    ingest = lw.IngestStore(source_kb / ".lumio" / "ingest")
    ingest.source_registry.register_source(
        "atlas-heatworks-product-catalog", b"atlas bytes", filename="atlas.pdf"
    )
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)

    # A title-shaped --source-id (the reader-trial failure shape): the error
    # names the close registered id (the bounded set is the pointer).
    rc = main(
        ["source", "inspect", str(source_kb), "--source-id", "Atlas Catalog Wrong"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "atlas-heatworks-product-catalog" in err
    # An invalid/possibly secret-bearing id is never echoed back; nothing is
    # close, so exactly ONE discovery command is named.
    rc = main(["source", "inspect", str(source_kb), "--source-id", "token=abc"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "token=abc" not in err
    assert "source list" in err


def test_source_fetch_unavailable_artifact_error_explains_retention_step(
    source_kb: Path, source_file: Path, tmp_path: Path, monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #174/#176: unavailable explains retention; unknown does not reach fetch."""
    # Registered but NOT retained: an empty store directory is configured.
    ingest = lw.IngestStore(source_kb / ".lumio" / "ingest")
    ingest.source_registry.register_source(
        "policy", INSPECTION_RAW, filename="policy.pdf"
    )
    store_root = tmp_path / "artifact-store"
    store_root.mkdir()
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))

    out_file = tmp_path / "fetched.pdf"
    rc = main(
        [
            "source",
            "fetch",
            str(source_kb),
            "--source-id",
            "policy",
            "--output",
            str(out_file),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "without artifact retention" in err
    assert "managed ingest" in err
    assert not out_file.exists()
