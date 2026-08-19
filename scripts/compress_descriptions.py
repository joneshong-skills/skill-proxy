#!/usr/bin/env python3
"""
Compress skill descriptions to keyword format for efficient CC auto-matching.

Replaces verbose "This skill should be used when..." descriptions with
compact comma-separated keywords (~12 tokens). CC can still match skills
directly, but system prompt token cost drops from ~50 to ~12 per skill.

Usage:
    compress_descriptions.py                 # Preview all compressions
    compress_descriptions.py --apply         # Write to SKILL.md files
    compress_descriptions.py --apply --dry-run  # Show diff without writing
    compress_descriptions.py --skill NAME    # Single skill
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SKILLS_DIR = Path.home() / ".claude/skills"
TRIGGERS_PATH = Path.home() / ".claude/data/skill-index/triggers.json"
BACKUP_PATH = Path.home() / ".claude/data/skill-index/description-backup.json"

# English stopwords to strip from trigger phrases
STOPWORDS = {
    "a",
    "an",
    "the",
    "to",
    "for",
    "of",
    "in",
    "on",
    "this",
    "that",
    "from",
    "or",
    "and",
    "is",
    "are",
    "it",
    "be",
    "do",
    "does",
    "did",
    "my",
    "me",
    "i",
    "you",
    "your",
    "we",
    "our",
    "they",
    "them",
    "has",
    "have",
    "had",
    "was",
    "were",
    "been",
    "being",
    "with",
    "at",
    "by",
    "as",
    "if",
    "when",
    "will",
    "can",
    "could",
    "should",
    "would",
    "may",
    "might",
    "shall",
    "must",
    "not",
    "no",
    "but",
    "so",
    "than",
    "then",
    "also",
    "just",
    "about",
    "up",
    "out",
    "all",
    "any",
    "each",
    "every",
    "some",
    "such",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "under",
    "over",
    "again",
    "further",
    "once",
    "here",
    "there",
    "where",
    "how",
    "what",
    "which",
    "who",
    "whom",
    "why",
    "its",
    "itself",
    "these",
    "those",
    "other",
    "more",
    "most",
    "own",
    "same",
    "new",
    "one",
    "two",
    "only",
    # CC-specific noise
    "skill",
    "used",
    "user",
    "asks",
    "mentions",
    "discusses",
    "use",
    "using",
    "wants",
    "something",
    "things",
    "thing",
    "need",
    "needs",
    "needed",
    "want",
    "like",
    "get",
    "make",
    "made",
    "way",
    "based",
    "related",
    "specific",
    "existing",
    "current",
    "currently",
    "various",
    "within",
    "across",
    "along",
    "whether",
    "whenever",
    "wherever",
    "whichever",
    "whatever",
    "however",
    "therefore",
    "otherwise",
    # File system noise
    "file",
    "files",
    "directory",
    "path",
    "folder",
    "downloads",
}

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")


def parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter into dict with proper multiline support."""
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return {}

    fm = {}
    raw = m.group(1)
    current_key = None
    current_val = []

    for line in raw.split("\n"):
        kv = re.match(r"^(\w[\w-]*):\s*(.*)", line)
        if kv:
            if current_key and current_val:
                fm[current_key] = " ".join(current_val).strip()
            current_key = kv.group(1)
            val = kv.group(2).strip()
            if val and val not in (">-", '""', "|"):
                current_val = [val]
            else:
                current_val = []
        elif current_key and line.strip():
            current_val.append(line.strip())

    if current_key and current_val:
        fm[current_key] = " ".join(current_val).strip()

    return fm


def resolve_description(skill_name: str, fm_desc: str) -> str:
    """Get the best available description for a skill.

    Priority: SKILL.md frontmatter > triggers.json > backup.
    """
    if fm_desc and len(fm_desc.strip('"').strip("'")) > 35:
        return fm_desc.strip('"').strip("'")

    # Try triggers.json
    if TRIGGERS_PATH.exists():
        entries = json.loads(TRIGGERS_PATH.read_text())
        for e in entries:
            if e["name"] == skill_name:
                desc = e.get("description", "")
                if desc and len(desc) > 35:
                    return desc
                break

    # Try backup
    if BACKUP_PATH.exists():
        backup = json.loads(BACKUP_PATH.read_text())
        if skill_name in backup and len(backup[skill_name]) > 35:
            return backup[skill_name]

    return fm_desc.strip('"').strip("'") if fm_desc else ""


def extract_keywords(description: str, skill_name: str) -> str:
    """Extract keyword string from a full description.

    Algorithm:
    1. Extract quoted trigger phrases → split into content words
    2. Extract CJK phrases (keep as-is)
    3. Score English words by frequency across phrases
    4. Take top 5-7 English + up to 4 CJK terms
    5. Join with ", "
    """
    if not description:
        # Fallback: name components
        return skill_name.replace("-", ", ")

    # Step 1: Extract quoted trigger phrases
    quoted = re.findall(r'"([^"]+)"', description)

    # Step 2: Separate CJK and English phrases
    cjk_phrases = []
    en_phrases = []

    for phrase in quoted:
        if CJK_RE.search(phrase):
            cjk_phrases.append(phrase)
        else:
            en_phrases.append(phrase)

    # Also extract CJK from the description body (not just quoted)
    if not cjk_phrases:
        cjk_matches = CJK_RE.findall(description)
        cjk_phrases = [m for m in cjk_matches if len(m) >= 2]

    # Step 3: Extract English content words and score by frequency
    word_freq: dict[str, int] = {}
    for phrase in en_phrases:
        words = re.findall(r"[a-z][a-z0-9]*", phrase.lower())
        for w in words:
            if w not in STOPWORDS and len(w) >= 2:
                word_freq[w] = word_freq.get(w, 0) + 1

    # If no quoted phrases, extract from prose
    if not word_freq:
        words = re.findall(r"[a-z][a-z0-9]+", description.lower())
        for w in words:
            if w not in STOPWORDS and len(w) >= 3:
                word_freq[w] = word_freq.get(w, 0) + 1

    # Step 4: Rank and select
    # Add skill name components with bonus frequency
    name_parts = skill_name.lower().replace("-", " ").split()
    for part in name_parts:
        if part not in STOPWORDS and len(part) >= 2:
            word_freq[part] = word_freq.get(part, 0) + 3

    sorted_words = sorted(word_freq.items(), key=lambda x: -x[1])
    top_en = [w for w, _ in sorted_words[:7]]

    # Deduplicate CJK: keep unique, max 4
    seen_cjk = set()
    top_cjk = []
    for phrase in cjk_phrases:
        if phrase not in seen_cjk and len(top_cjk) < 4:
            seen_cjk.add(phrase)
            top_cjk.append(phrase)

    # Step 5: Combine
    keywords = top_en + top_cjk

    if not keywords:
        return skill_name.replace("-", ", ")

    return ", ".join(keywords)


def replace_description(content: str, new_desc: str) -> str:
    """Replace the description field in YAML frontmatter."""
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return content

    fm = m.group(1)
    lines = fm.split("\n")
    new_lines = []
    in_desc = False

    for line in lines:
        if line.startswith("description:"):
            new_lines.append(f'description: "{new_desc}"')
            in_desc = True
        elif in_desc and (line.startswith("  ") or line.startswith("\t")):
            continue  # Skip multiline continuation
        else:
            in_desc = False
            new_lines.append(line)

    new_fm = "\n".join(new_lines)
    return content[: m.start(1)] + new_fm + content[m.end(1) :]


def process_all(
    target_skill: str | None = None,
    apply: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """Process all skills and return results."""
    results = []

    # Method-C: core skills listed in hot-skills.json keep their FULL description
    # (high-frequency + safety-net, autonomous-trigger precision matters); only the
    # long tail gets compressed to keyword lists. An explicit --skill overrides this.
    core = set()
    hot_path = Path.home() / ".claude/data/skill-index/hot-skills.json"
    if hot_path.exists():
        core = set(json.loads(hot_path.read_text()))

    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
            continue

        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            continue

        if target_skill and d.name != target_skill:
            continue

        if d.name in core and not target_skill:  # core keeps full desc
            continue

        content = skill_md.read_text()
        fm = parse_frontmatter(content)
        fm_desc = fm.get("description", "")
        full_desc = resolve_description(d.name, fm_desc)
        keywords = extract_keywords(full_desc, d.name)

        result = {
            "name": d.name,
            "original": full_desc[:80] + "..." if len(full_desc) > 80 else full_desc,
            "keywords": keywords,
            "tokens_est": len(keywords.split()) + len(keywords.split(",")),
        }
        results.append(result)

        if apply and not dry_run:
            new_content = replace_description(content, keywords)
            skill_md.write_text(new_content)

    return results


def main():
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    target = None
    if "--skill" in args:
        idx = args.index("--skill")
        target = args[idx + 1]
        args = [a for i, a in enumerate(args) if i != idx and i != idx + 1]

    apply = "--apply" in args
    dry_run = "--dry-run" in args

    results = process_all(target_skill=target, apply=apply, dry_run=dry_run)

    if not results:
        print("No skills found.")
        sys.exit(1)

    # Display results
    prefix = "[DRY RUN] " if dry_run else "[APPLY] " if apply else "[PREVIEW] "
    total_tokens = 0

    for r in results:
        total_tokens += r["tokens_est"]
        print(f"{prefix}{r['name']}")
        print(f"  Original: {r['original']}")
        print(f"  Keywords: {r['keywords']}")
        print(f"  ~{r['tokens_est']} tokens")
        print()

    print(f"{'─' * 50}")
    print(f"Total skills: {len(results)}")
    print(f"Estimated total tokens: ~{total_tokens}")

    if apply and not dry_run:
        print(f"\nApplied to {len(results)} SKILL.md files.")
    elif dry_run:
        print("\nDry run — no files modified.")


if __name__ == "__main__":
    main()
