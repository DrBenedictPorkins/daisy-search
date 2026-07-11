#!/bin/bash
# Create a new Daisy Search investigation directory
# Usage: ./scripts/new_search.sh "<name>" "<context>"

set -e

DAISY_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECTS_DIR="$DAISY_ROOT/projects"
TEMPLATES_DIR="$DAISY_ROOT/templates"

if [ -z "$1" ]; then
    echo "Usage: $0 <subject_name> [description]"
    echo "Example: $0 \"<name>\" \"<context>\""
    exit 1
fi

SUBJECT_NAME="$1"
DESCRIPTION="${2:-investigation}"
TODAY=$(date +%Y-%m-%d)

# Create slug from name (matches daisy MCP server _slugify logic)
SLUG=$(echo "$SUBJECT_NAME" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-')

PROJECT_DIR="$PROJECTS_DIR/$SLUG"

if [ -d "$PROJECT_DIR" ]; then
    echo "Project directory already exists: $PROJECT_DIR"
    echo "To resume: cd $PROJECT_DIR && claude"
    exit 1
fi

echo "Creating search: $SUBJECT_NAME ($DESCRIPTION)"
echo "Directory: $PROJECT_DIR"

# Create directory structure
mkdir -p "$PROJECT_DIR"/{notes,downloads,screenshots,reports,raw,.claude}

# Generate CLAUDE.md from template
sed \
    -e "s|{{SUBJECT_NAME}}|$SUBJECT_NAME|g" \
    -e "s|{{SUBJECT_SLUG}}|$SLUG|g" \
    -e "s|{{DESCRIPTION}}|$DESCRIPTION|g" \
    -e "s|{{DATE}}|$TODAY|g" \
    -e "s|{{PROJECT_ID}}|(run daisy_new_project to get this)|g" \
    -e "s|{{DAISY_ROOT}}|$DAISY_ROOT|g" \
    "$TEMPLATES_DIR/CLAUDE.md.template" > "$PROJECT_DIR/CLAUDE.md"

# Generate SESSION_STATE.md from template
sed \
    -e "s|{{SUBJECT_NAME}}|$SUBJECT_NAME|g" \
    -e "s|{{SUBJECT_SLUG}}|$SLUG|g" \
    -e "s|{{DESCRIPTION}}|$DESCRIPTION|g" \
    -e "s|{{DATE}}|$TODAY|g" \
    -e "s|{{PROJECT_ID}}|(not yet created)|g" \
    -e "s|{{DAISY_ROOT}}|$DAISY_ROOT|g" \
    "$TEMPLATES_DIR/SESSION_STATE.md.template" > "$PROJECT_DIR/notes/SESSION_STATE.md"

# Generate .claude/settings.local.json with MCP server config
cat > "$PROJECT_DIR/.claude/settings.local.json" << SETTINGSEOF
{
  "mcpServers": {
    "daisy-search": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "$DAISY_ROOT", "run", "daisy-mcp"],
      "env": {}
    }
  },
  "permissions": {
    "allow": [
      "mcp__daisy-search__*",
      "mcp__claude-in-chrome__*",
      "Read(*)",
      "Write($PROJECT_DIR/*)",
      "Glob(*)",
      "Grep(*)"
    ]
  }
}
SETTINGSEOF

echo ""
echo "Project created: $PROJECT_DIR"
echo ""
echo "To start:"
echo "  cd $PROJECT_DIR"
echo "  claude"
echo ""
echo "Claude will read CLAUDE.md and have access to Daisy Search MCP tools."
echo "First step: run daisy_new_project to create the project in the database."
