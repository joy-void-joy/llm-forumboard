#!/bin/bash
# File suggestion provider for Claude Code @ completion.
#
# Wired by .claude/settings.json (`fileSuggestion.command`), so Claude Code
# runs it automatically — no install step. It just needs jq, fzf, and git on
# PATH; Claude Code pipes a {"query": ...} JSON payload on stdin.
#
# Suggestions are git-tracked files (git ls-files) plus any symlinked refs/
# entries, fuzzy-matched by fzf. A missing refs/ directory or zero matches
# yields empty output and exit 0.
#
# Canonical source: src/lup_template/devtools/harness/content/assets/
# file_suggest.sh, copied into the plugin tree by
# `uv run lup-devtools harness generate all` — edit it there.

set -euo pipefail

QUERY=$(jq -r '.query // ""')
cd "${CLAUDE_PROJECT_DIR:-.}"

{
  git --no-pager ls-files
  if [ -d refs ]; then
    for d in refs/*/; do
      if [ -L "${d%/}" ]; then
        echo "${d%/}"
      fi
    done
  fi
} | fzf --filter "$QUERY" | head -15 || true
