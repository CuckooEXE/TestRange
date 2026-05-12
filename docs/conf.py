"""Sphinx configuration for testrange."""

from __future__ import annotations

project = "testrange"
author = "testrange contributors"
copyright = f"{author}"  # noqa: A001 — Sphinx reads this name

extensions = [
    "myst_parser",
    "sphinx_copybutton",
]

source_suffix = {
    ".md": "markdown",
    ".rst": "restructuredtext",
}

# Recognized by myst-parser; corresponds to common Markdown extensions
# the existing .md files use.
myst_enable_extensions = [
    "colon_fence",   # ::: fence directive form
    "deflist",       # definition lists
    "smartquotes",   # typographic quotes
]
myst_heading_anchors = 3

html_theme = "furo"
html_title = "testrange"
html_static_path = ["_static"]

# Sphinx 9.x dropped the implicit "include common metadata" default — silence
# the "no master document" warning by being explicit.
master_doc = "index"

# Don't warn about missing references for the ADR cross-links during initial
# bootstrap; the ADRs reference each other and external URLs.
suppress_warnings = ["myst.header"]

# Copy-button shouldn't include the prompt or output lines.
copybutton_prompt_text = r">>> |\.\.\. |\$ "
copybutton_prompt_is_regexp = True
