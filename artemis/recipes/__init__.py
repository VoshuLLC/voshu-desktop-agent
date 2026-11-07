"""Artemis Recipes — record once, replay deterministically without an LLM.

A recipe is the distilled action spine of a recorded session: the ordered
wire-level device calls (canonical ``ActionSpec`` names + wire args) that a
successful run dispatched. ``RecipeRecorder`` mines a session's stored steps
and traces into a ``Recipe``; ``RecipeRunner`` replays it through the same
in-process action session the Validator uses, substituting ``{{variables}}``
in string arguments per run. Recipes are JSON-persisted under
``traces/recipes/``.
"""
