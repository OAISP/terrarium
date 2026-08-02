#!/usr/bin/env bash
# Manage the skills/ directory. Skills here are mounted read-only into any agent
# whose harness has `skills: true` (loaded via the project setting source).
# Curate them in git; no image rebuild needed.
#
#   scripts/skill.sh list
#   scripts/skill.sh new <name>
set -euo pipefail

SKILLS_DIR="${TERRA_SKILLS_DIR:-$(cd "$(dirname "$0")/.." && pwd)/skills}"

case "${1:-list}" in
  list)
    echo "skills in $SKILLS_DIR:"
    find "$SKILLS_DIR" -maxdepth 1 -mindepth 1 -type d -printf "  %f\n" 2>/dev/null || echo "  (none)"
    ;;
  new)
    name="${2:?usage: skill.sh new <name>}"
    dir="$SKILLS_DIR/$name"
    [ -e "$dir" ] && { echo "already exists: $dir"; exit 1; }
    mkdir -p "$dir"
    cat > "$dir/SKILL.md" <<EOF
---
name: $name
description: One line describing WHEN this skill applies (always in context).
---

Write the skill's instructions here. The agent reads this body when the task is
relevant to the description above. You can also add supporting files alongside
SKILL.md (scripts, references) that the agent may read.
EOF
    echo "created $dir/SKILL.md — edit it, then enable skills on an agent."
    ;;
  *)
    echo "usage: skill.sh [list|new <name>]"; exit 1
    ;;
esac
