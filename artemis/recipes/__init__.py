"""Artemis Recipes — record once, replay deterministically without an LLM.

A recipe is the distilled action spine of a recorded session: the ordered
wire-level device calls (canonical ``ActionSpec`` names + wire args) that a
successful run dispatched. ``RecipeRecorder`` mines a session's stored steps
and traces into a ``Recipe``; ``RecipeRunner`` replays it through the same
in-process action session the Validator uses, substituting ``{{variables}}``
in string arguments per run. Recipes are JSON-persisted under
``traces/recipes/``.

Every device also gets a durable workspace under ``traces/workspaces/
<serial>/`` (``notes.md`` / ``assets/`` / ``memories.md`` / ``coords.json``):
the runner ensures it before each run, resolves ``@workspace`` file
references in step args, stages files via the ``inject_file`` step action,
and appends one memory line after the run. Steps stay deterministic unless
they opt into ``on_fail: "vision"`` — one bounded VLM repair call that, on
success, writes the fix back into the recipe.
"""
