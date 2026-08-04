# Prior Lumio Wiki wheel fixture

`lumio_wiki-0.1.1-py3-none-any.whl` is the genuine wheel built from repository
commit `d0bfcdd` immediately before issue #150 changed skill distribution.
It intentionally contains the legacy manifest-less install behavior and the
former split resource layout (`data/skill/SKILL.md` plus
`data/protocol/PROTOCOL.md`).

Rebuild from the repository root with:

```bash
tmp=$(mktemp -d)
git archive d0bfcdd packages/lumio-wiki | tar -x -C "$tmp"
uv build --wheel --out-dir "$tmp/wheels" "$tmp/packages/lumio-wiki"
```

Expected SHA-256:
`043bc432fa361ef2a7827644f84c77b2cfb7c8c13cbcae1fae61cc9ddf9e113a`.
