"""AI pipeline layers (metadata filter → Moondream audit → aesthetic gate).

Import submodules explicitly, e.g. ``greeceapt.ai_agent.ai_conductor``.
Avoid re-exporting ``run`` here so ``python -m greeceapt.ai_agent.ai_conductor``
does not hit a half-initialized package.
"""
