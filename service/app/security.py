"""Input vetting for caller-supplied ``oh`` CLI flags.

The ``extra_oh_args`` field lets API clients forward extra flags to the ``oh``
CLI. Because ``--permission-mode full_auto`` (and ``--output``) are emitted
*before* ``extra_args`` in :mod:`app.workers.runner`, a caller could otherwise
append a conflicting flag and downgrade permissions or redirect artifacts.

We therefore keep a conservative allowlist of safe flags and a hard blocklist of
safety-critical flags that must never be caller-controlled.
"""

from __future__ import annotations

# flag -> does it consume a following value?
ALLOWED_OH_FLAGS: dict[str, bool] = {
    "--temperature": True,
    "--max-turns": True,
    "--model": True,
    "--no-cache": False,
    "--verbose": False,
    # ⚠️ Only add flags that are provably safe to expose to callers.
}

# Flags that must never be caller-controlled.
FORBIDDEN_OH_FLAGS = {
    "--permission-mode",
    "--permission_mode",
    "--output",
    "--output-format",
    "-p",
    "--prompt",
    "--workspace",
    "--cwd",
    "--root",
    "--headed",
    "--no-headless",  # could pop a GUI / change browser behavior
    "--browser",
    "--chromium",
}


class InvalidOhArgError(ValueError):
    """Raised when ``extra_oh_args`` contains a disallowed or malformed token."""


def vet_extra_oh_args(raw: list[str] | None) -> list[str]:
    """Validate and normalize ``extra_oh_args``.

    Args:
        raw: The caller-supplied list of extra CLI tokens (may be ``None``).

    Returns:
        A sanitized copy of the list, ready to be forwarded to ``oh``.

    Raises:
        InvalidOhArgError: if any token is not a ``--flag``, is on the
            forbidden list, is not in the allowlist, or is missing a required
            value.
    """
    if not raw:
        return []

    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        tok = raw[i]
        if not isinstance(tok, str) or not tok.startswith("--"):
            raise InvalidOhArgError(f"only --flags are allowed, got {tok!r}")
        if tok in FORBIDDEN_OH_FLAGS:
            raise InvalidOhArgError(f"flag {tok!r} is not caller-controllable")
        if tok not in ALLOWED_OH_FLAGS:
            raise InvalidOhArgError(f"flag {tok!r} is not in the allowlist")
        out.append(tok)
        if ALLOWED_OH_FLAGS[tok]:
            if i + 1 >= n:
                raise InvalidOhArgError(f"flag {tok!r} requires a value")
            out.append(raw[i + 1])
            i += 2
        else:
            i += 1
    return out
