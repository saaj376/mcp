"""Phase 5: repository governance — CI gates, branch protection, org rulesets.

See ``docs/governance-design.md``. The design in one line: the CHECKS registry in
``checks.py`` is the single source of truth, read by the local runner, the
workflow generator, and the branch-protection payload alike.
"""
