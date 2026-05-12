# Minimal Sphinx Makefile.
#
# Usage:
#   make html       # build HTML into _build/html/
#   make clean      # remove _build/
#   make linkcheck  # offline link verification
#
SPHINXBUILD ?= sphinx-build
SOURCEDIR   := .
BUILDDIR    := _build

.PHONY: help html clean linkcheck

help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)"

html:
	@$(SPHINXBUILD) -M html "$(SOURCEDIR)" "$(BUILDDIR)"

clean:
	rm -rf "$(BUILDDIR)"

linkcheck:
	@$(SPHINXBUILD) -M linkcheck "$(SOURCEDIR)" "$(BUILDDIR)"
