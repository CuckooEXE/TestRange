"""Sphinx configuration for the TestRange documentation."""

from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable without installing it
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Project information
# ---------------------------------------------------------------------------

project = "TestRange"
copyright = "2024, TestRange Contributors"
author = "TestRange Contributors"

try:
    from testrange._version import __version__ as _ver
    release = _ver
    version = ".".join(_ver.split(".")[:2])
except ImportError:
    release = version = "0.1.0"

# ---------------------------------------------------------------------------
# General configuration
# ---------------------------------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# ---------------------------------------------------------------------------
# autodoc settings
# ---------------------------------------------------------------------------

autodoc_typehints = "description"
autodoc_typehints_format = "short"
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
}

napoleon_google_docstring = False
napoleon_numpy_docstring = False
napoleon_use_param = True
napoleon_use_returns = True
napoleon_use_rtype = False

# ---------------------------------------------------------------------------
# intersphinx
# ---------------------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# ---------------------------------------------------------------------------
# HTML output
# ---------------------------------------------------------------------------

html_theme = "furo"
html_title = f"TestRange {version}"
html_static_path = ["_static"]

html_theme_options = {
    "sidebar_hide_name": False,
    "navigation_with_keys": True,
    "light_css_variables": {
        "color-brand-primary": "#0a6cbf",
        "color-brand-content": "#0a6cbf",
    },
    "dark_css_variables": {
        "color-brand-primary": "#4ea6e8",
        "color-brand-content": "#4ea6e8",
    },
}
