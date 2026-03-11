---
name: skill-proxy
description: ""
version: "1.0"
tags: meta, proxy, skill-discovery
---

# Skill Proxy

Discover and invoke cold skills that have no description loaded in the system prompt.
~80% of skills are "cold" (descriptions stripped to save tokens). This proxy searches
the skill index to find the best match for any unmatched user request.

## When to Use

- User's request doesn't match any hot skill
- User asks "is there a skill for X?"
- User mentions a capability that sounds like it could be a skill

## Workflow

### Step 1: Search the index

```bash
~/.local/bin/python3 ~/.claude/skills/skill-proxy/scripts/match_skill.py "USER_QUERY_HERE"
```

Output: JSON with top 3 matches (name, score, description).

### Step 2: Evaluate matches

- Score >= 8: high confidence → invoke directly
- Score 4-7: medium → show user the match, ask to confirm
- Score < 4: low → tell user no strong match found

### Step 3: Invoke the matched skill

Use the Skill tool with the matched skill name. The cold skill's SKILL.md still
exists (just without description), so the Skill tool can invoke it normally.

```
Skill(skill: "matched-skill-name", args: "original user request")
```

## Management Commands

```bash
# Check current hot/cold state
~/.local/bin/python3 ~/.claude/skills/skill-proxy/scripts/apply_cold.py status

# Strip cold skill descriptions (apply compression)
~/.local/bin/python3 ~/.claude/skills/skill-proxy/scripts/apply_cold.py apply

# Restore all descriptions (before editing/publishing skills)
~/.local/bin/python3 ~/.claude/skills/skill-proxy/scripts/apply_cold.py restore

# Restore single skill
~/.local/bin/python3 ~/.claude/skills/skill-proxy/scripts/apply_cold.py restore diagram-gen

# View hot skills list
~/.local/bin/python3 ~/.claude/skills/skill-proxy/scripts/match_skill.py --hot

# Show stats
~/.local/bin/python3 ~/.claude/skills/skill-proxy/scripts/match_skill.py --stats
```

## Architecture

```
~/.claude/skills/
├── smart-search/SKILL.md    ← Hot: full description (auto-matched by Claude)
├── diagram-gen/SKILL.md     ← Cold: description="" (discoverable via proxy)
└── skill-proxy/SKILL.md     ← This skill: search index + management

~/.claude/data/skill-index/
├── triggers.json            ← Search index (81+ skills, triggers/domain/tags)
├── hot-skills.json          ← List of 20 hot skill names
└── description-backup.json  ← Backup of stripped descriptions
```

## Updating Hot/Cold Classification

Edit `~/.claude/data/skill-index/hot-skills.json` to change which skills keep
their descriptions. Then run `apply_cold.py apply` to strip the rest.

Rebuild the search index after adding new skills:
```bash
~/.local/bin/python3 ~/.claude/data/skill-index/build-triggers.py
```
