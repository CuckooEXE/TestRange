"""Sphinx configuration for testrange."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

project = "testrange"
author = "testrange contributors"
copyright = f"2026, {author}"

try:
    release = _pkg_version("testrange")
except PackageNotFoundError:  # docs built without the package installed
    release = ""
version = ".".join(release.split(".")[:2])

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
    "colon_fence",  # ::: fence directive form
    "deflist",  # definition lists
    "smartquotes",  # typographic quotes
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
