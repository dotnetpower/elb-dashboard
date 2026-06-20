"""Environment-variable parsing helpers shared across services and tasks.

Responsibility: Provide one canonical, side-effect-free reader for integer
environment knobs so modules stop re-defining their own ``_env_int`` clone.
Edit boundaries: Keep this module limited to pure env-parsing helpers — no
Azure SDK, no logging side effects, no I/O. Add new typed readers here
(``env_bool``, ``env_float``) rather than re-deriving them per module.
Key entry points: ``env_int``.
Risky contracts: ``env_int`` returns ``default`` for unset / empty / unparseable
values (never raises), and clamps to ``[minimum, maximum]`` only when those
bounds are provided. Callers that previously relied on a per-module default
``minimum`` MUST pass it explicitly here; the shared helper does not clamp
unless told to.
Validation: `uv run pytest -q api/tests/test_env.py`.
"""

from __future__ import annotations

import os


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read an integer environment knob, returning ``default`` on any problem.

    The value is returned unchanged when it parses cleanly; an unset, empty,
    or non-integer value falls back to ``default`` (so a typo never silently
    disables a timeout). When ``minimum`` / ``maximum`` are supplied the parsed
    value is clamped into that inclusive range.

    Read at call time (not import) so a deployed revision can be reconfigured
    via env without a code change.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value
