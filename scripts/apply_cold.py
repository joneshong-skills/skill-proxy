#!/usr/bin/env python3
"""
Apply/restore cold skill descriptions.

Usage:
    apply_cold.py apply              # Compact cold skill descriptions
    apply_cold.py restore [name]     # Restore via git checkout (all or one)
    apply_cold.py status             # Show current state
    apply_cold.py diff               # Show what would change

How it works:
- 'apply' replaces verbose descriptions with a short name-derived summary
  (compatible with Claude Code, Gemini CLI, and Codex CLI)
- Full descriptions are always recoverable via 'restore' (git checkout)
- Hot skills are never touched
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SKILLS_DIR = Path.home() / ".claude/skills"
HOT_SKILLS_PATH = Path.home() / ".claude/data/skill-index/hot-skills.json"
BACKUP_DIR = Path.home() / ".claude/data/skill-index/description-backup.json"


def load_hot_skills() -> list[str]:
    if HOT_SKILLS_PATH.exists():
        return json.loads(HOT_SKILLS_PATH.read_text())
    return []


def get_description_from_frontmatter(content: str) -> str | None:
    """Extract description value from YAML frontmatter."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return None

    fm = match.group(1)
    desc = ""
    in_desc = False
    for line in fm.split("\n"):
        if line.startswith("description:"):
            in_desc = True
            val = line.split(":", 1)[1].strip()
            if val and val != ">-" and val != '""':
                desc = val
            else:
                desc = ""
        elif in_desc and (line.startswith("  ") or line.startswith("\t")):
            desc += " " + line.strip()
        elif in_desc:
            in_desc = False

    return desc.strip().strip('"') if in_desc or desc else desc.strip().strip('"')


def _name_to_short_desc(name: str) -> str:
    """Convert skill name to short Codex-compatible description."""
    return name.replace("-", " ").replace("_", " ").title()


def strip_description(content: str, skill_name: str = "") -> str:
    """Replace description with short name-derived summary in YAML frontmatter.

    Uses skill name as minimal description instead of "" for Codex CLI compatibility.
    """
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return content

    short_desc = _name_to_short_desc(skill_name) if skill_name else "skill"
    fm = match.group(1)
    lines = fm.split("\n")
    new_lines = []
    in_desc = False

    for line in lines:
        if line.startswith("description:"):
            new_lines.append(f'description: "{short_desc}"')
            in_desc = True
        elif in_desc and (line.startswith("  ") or line.startswith("\t")):
            continue  # Skip multiline description continuation
        else:
            in_desc = False
            new_lines.append(line)

    new_fm = "\n".join(new_lines)
    return content[: match.start(1)] + new_fm + content[match.end(1) :]


def apply_cold(dry_run: bool = False) -> dict:
    """Strip descriptions from cold skills. Returns stats."""
    hot_skills = load_hot_skills()
    # Fail-closed: 空白名單會把所有 skill（含高頻 hot）全部 strip，屬災難性操作。
    if not hot_skills:
        raise SystemExit(
            "ABORT: hot-skills.json 不存在或為空 — apply 會 cold 化全部 skill。"
            "先建立 hot 白名單再執行。"
        )
    stats = {
        "stripped": 0,
        "skipped_hot": 0,
        "skipped_no_desc": 0,
        "skipped_already": 0,
        "errors": 0,
    }
    backup = {}

    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
            continue

        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            continue

        if d.name in hot_skills:
            stats["skipped_hot"] += 1
            continue

        content = skill_md.read_text()
        desc = get_description_from_frontmatter(content)

        if desc is None:
            stats["skipped_no_desc"] += 1
            continue

        expected_short = _name_to_short_desc(d.name)
        if desc == expected_short:
            stats["skipped_already"] += 1
            continue

        # Backup original description
        backup[d.name] = desc

        if not dry_run:
            new_content = strip_description(content, skill_name=d.name)
            skill_md.write_text(new_content)

        stats["stripped"] += 1

    # Save backup
    if backup and not dry_run:
        BACKUP_DIR.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if BACKUP_DIR.exists():
            existing = json.loads(BACKUP_DIR.read_text())
        existing.update(backup)
        BACKUP_DIR.write_text(json.dumps(existing, indent=2, ensure_ascii=False))

    return stats


def restore(skill_name: str | None = None) -> dict:
    """Restore descriptions via git checkout."""
    hot_skills = load_hot_skills()
    stats = {"restored": 0, "errors": 0}

    targets = []
    if skill_name:
        skill_dir = SKILLS_DIR / skill_name
        if skill_dir.is_dir():
            targets.append(skill_dir)
        else:
            print(f"Error: skill '{skill_name}' not found", file=sys.stderr)
            return {"restored": 0, "errors": 1}
    else:
        for d in sorted(SKILLS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
                continue
            if d.name in hot_skills:
                continue
            targets.append(d)

    for d in targets:
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            continue

        # Try git checkout
        try:
            result = subprocess.run(
                ["git", "checkout", "--", "SKILL.md"],
                cwd=str(d),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                stats["restored"] += 1
            else:
                # Fallback: regex-replace the CURRENT description (name-title
                # placeholder OR empty) with the backed-up full one. The old code
                # looked for literal 'description: ""' but apply writes a name-title
                # ("Anvil"), so the replace was a no-op yet still reported success.
                if BACKUP_DIR.exists():
                    backup = json.loads(BACKUP_DIR.read_text())
                    if d.name in backup:
                        content = skill_md.read_text()
                        new_content = re.sub(
                            r"^description:.*(?:\n[ \t]+.*)*",
                            lambda m: "description: >-\n  " + backup[d.name],
                            content,
                            count=1,
                            flags=re.M,
                        )
                        if new_content != content:  # only count if it actually changed
                            skill_md.write_text(new_content)
                            stats["restored"] += 1
                        else:
                            stats["errors"] += 1
                    else:
                        stats["errors"] += 1
                else:
                    stats["errors"] += 1
        except (subprocess.TimeoutExpired, OSError):
            stats["errors"] += 1

    return stats


def show_status():
    """Show current tier state (Method-C: core full desc + long-tail keyword lists)."""
    hot_skills = load_hot_skills()
    total = 0
    hot = 0
    cold_stripped = 0
    cold_full = 0

    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
            continue
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            continue

        total += 1
        content = skill_md.read_text()
        desc = get_description_from_frontmatter(content)

        expected_short = _name_to_short_desc(d.name)
        if d.name in hot_skills:
            hot += 1
        elif desc == "" or desc == '""' or desc is None or desc == expected_short:
            cold_stripped += 1
        else:
            cold_full += 1

    print(f"Total skills:          {total}")
    print(f"Core (full desc):      {hot}")
    print(f"Long-tail (keyword):   {cold_full}")
    if cold_stripped > 0:
        print(
            f"Legacy name-title:     {cold_stripped}  "
            "← run compress_descriptions.py --apply to convert these to keyword lists"
        )
    else:
        print("\nMethod-C: core full desc + long-tail keyword lists (no legacy name-title strips).")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "apply":
        dry_run = "--dry-run" in sys.argv
        stats = apply_cold(dry_run=dry_run)
        prefix = "[DRY RUN] " if dry_run else ""
        print(f"{prefix}Stripped:        {stats['stripped']}")
        print(f"{prefix}Skipped (hot):   {stats['skipped_hot']}")
        print(f"{prefix}Skipped (empty): {stats['skipped_already']}")
        print(f"{prefix}Skipped (no FM): {stats['skipped_no_desc']}")
        if not dry_run and stats["stripped"] > 0:
            print(f"\nBackup saved to {BACKUP_DIR}")
            print("Restore with: apply_cold.py restore")

    elif cmd == "restore":
        name = sys.argv[2] if len(sys.argv) > 2 else None
        stats = restore(name)
        print(f"Restored: {stats['restored']}")
        if stats["errors"]:
            print(f"Errors:   {stats['errors']}")

    elif cmd == "status":
        show_status()

    elif cmd == "diff":
        stats = apply_cold(dry_run=True)
        print(f"Would strip {stats['stripped']} cold skill descriptions")
        print(f"Would skip {stats['skipped_hot']} hot skills")

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
