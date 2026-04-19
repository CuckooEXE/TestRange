# Sphinx documentation Makefile for TestRange.
#
# Usage:
#   make html    — build HTML into _build/html/
#   make clean   — remove the entire _build/ directory
#   make help    — list every builder Sphinx knows about
#
# Override SPHINXBUILD if your environment prefers a different entry
# point (e.g. ``SPHINXBUILD="uv run sphinx"``).

SPHINXOPTS    ?=
SPHINXBUILD   ?= python3 -m sphinx
SOURCEDIR     = .
BUILDDIR      = _build

.PHONY: help clean Makefile

help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

clean:
	rm -rf "$(BUILDDIR)"

# Catch-all: delegate anything else (html, linkcheck, doctest, latex, …)
# to Sphinx's make-mode so every builder it ships works out of the box.
%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
