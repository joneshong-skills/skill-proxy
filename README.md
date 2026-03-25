[English](README.md) | [繁體中文](README.zh.md)

# skill-proxy

Discover and invoke cold skills that have no description loaded in the active system prompt.

## Description

Skill Proxy solves the token-budget problem of large skill libraries. About 80% of skills are "cold" — their descriptions are stripped to save context tokens. When a user request doesn't match any hot skill, Skill Proxy searches a pre-built trigger index to find the best match and invokes it transparently, along with managing which skills are hot or cold at any time.

## Features

- Fuzzy trigger-index search across 81+ installed skills
- Confidence-scored matching (high / medium / low thresholds)
- Seamless cold-skill invocation via the Skill tool
- Hot/cold state management: apply, restore, and status commands
- Per-skill restore for editing or publishing workflows
- Search index rebuild after new skill installation

## Usage

```
/skill-proxy
```

Trigger phrases:
- "is there a skill for X?"
- Any user request that doesn't match a currently loaded hot skill
- "what skills do you have for diagrams?"

## How It Works

The proxy runs `match_skill.py` against the `~/.claude/data/skill-index/triggers.json` index and returns the top 3 candidate skills with confidence scores. A score >= 8 triggers direct invocation; scores 4–7 prompt user confirmation; scores below 4 report no strong match. The matched cold skill's `SKILL.md` still exists on disk, so the Skill tool can invoke it normally without requiring its description to be loaded in the system prompt.

## Requirements

- Claude Code CLI
- `~/.claude/data/skill-index/triggers.json` (built by `build-triggers.py`)
- `~/.local/bin/python3` (uv-managed Python 3.12)

## License

MIT
