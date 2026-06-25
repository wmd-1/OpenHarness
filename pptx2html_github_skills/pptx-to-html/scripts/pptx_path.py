#!/usr/bin/env python3
"""Helpers for normalizing PPTX internal zip paths from relationship Targets.

PPTX relationship Targets come in several forms (slide-relative, parent-relative,
or already absolute). The rest of this skill reads zip members by a single
canonical path rooted under ``ppt/``; ``normalize_pptx_path`` returns that path
without double-prefixing, fixing the ``ppt/ppt/charts/chart1.xml`` KeyError seen
when a Target is already absolute (common in WPS / Google Slides exports).
"""

from __future__ import annotations


def normalize_pptx_path(target: str) -> str:
    """Normalize a relationship Target into a canonical zip-internal path.

    Examples:
      'charts/chart1.xml'       -> 'ppt/charts/chart1.xml'
      '../charts/chart1.xml'    -> 'ppt/charts/chart1.xml'
      'ppt/charts/chart1.xml'   -> 'ppt/charts/chart1.xml'  (no double prefix)
      '/ppt/charts/chart1.xml'  -> 'ppt/charts/chart1.xml'
    """
    t = target.replace("..", "").lstrip("/").replace("//", "/")
    return t if t.startswith("ppt/") else f"ppt/{t}"
