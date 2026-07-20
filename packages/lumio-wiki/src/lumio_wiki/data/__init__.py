"""Package data root for the lumio-wiki Agent Skill and coding-agent protocol.

The ``skill/`` and ``protocol/`` subdirectories ship the reusable Agent
Skill (``SKILL.md``) and the short coding-agent protocol (``PROTOCOL.md``)
as wheel data. They are resolved via :mod:`importlib.resources` by
:mod:`lumio_wiki.skill` so a coding agent can locate them deterministically
from the built wheel without cloning the Lumio repository (issue #98).
"""
