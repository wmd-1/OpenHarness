"""Shared YAML frontmatter parsing for SKILL.md files."""

from __future__ import annotations

import logging

import yaml

logger = logging.getLogger(__name__)


def parse_skill_frontmatter(
    default_name: str,
    content: str,
    *,
    fallback_template: str = "Skill: {name}",
) -> tuple[str, str]:
    """Extract ``name`` and ``description`` from a SKILL.md file.

    Parses YAML frontmatter (``---`` delimited) via ``yaml.safe_load`` so that
    folded block scalars (``>``), literal block scalars (``|``), quoted values,
    and other standard YAML constructs are handled correctly. Falls back to
    ``# heading`` + first body paragraph when no usable frontmatter is present,
    and finally to ``fallback_template`` when no description can be derived.
    """
    name = default_name
    description = ""
    lines = content.splitlines()

    if content.startswith("---\n"):
        end_index = content.find("\n---\n", 4)
        if end_index != -1:
            try:
                metadata = yaml.safe_load(content[4:end_index])
                if isinstance(metadata, dict):
                    val = metadata.get("name")
                    if isinstance(val, str) and val.strip():
                        name = val.strip()
                    val = metadata.get("description")
                    if isinstance(val, str) and val.strip():
                        description = val.strip()
            except yaml.YAMLError:
                logger.debug("Failed to parse YAML frontmatter for skill %s", default_name)

    if not description:
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# "):
                if not name or name == default_name:
                    name = stripped[2:].strip() or default_name
                continue
            if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                description = stripped[:200]
                break

    if not description:
        description = fallback_template.format(name=name)
    return name, description
