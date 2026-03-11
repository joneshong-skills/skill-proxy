#!/usr/bin/env python3
"""
Skill Proxy — query-based skill matching against triggers.json index.

Usage:
    match_skill.py "user query here"
    match_skill.py --top 5 "create a diagram"
    match_skill.py --hot          # list current hot skills
    match_skill.py --stats        # show hot/cold breakdown

Scoring layers:
  1. Exact trigger phrase match:    +10
  2. Trigger substring match:       +5
  3. Name match (exact/contains):   +8 / +5
  4. Name component match:          +5 per component
  5. Prefix/stem match on triggers: +3
  6. Description keyword match:     +2 per keyword
  7. Trigger keyword match:         +1.5 per keyword
  8. Domain match:                  +3
  9. Tag match:                     +2 per tag
  10. CJK overlap on triggers:      up to +6
  11. CJK overlap on description:   up to +3
  12. Alias expansion:              injects synthetic tokens
  13. Intent signals:               +6 per match
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TRIGGERS_PATH = Path.home() / ".claude/data/skill-index/triggers.json"
HOT_SKILLS_PATH = Path.home() / ".claude/data/skill-index/hot-skills.json"

# ── Alias expansion: abbreviations/synonyms → canonical skill names ──
# When a query token matches an alias key, the alias values are injected
# as synthetic tokens, giving the target skill a strong name-match boost.
ALIASES = {
    # Format abbreviations
    "ppt": ["pptx"],
    "簡報": ["pptx", "presentation"],
    "doc": ["docx"],
    "xls": ["xlsx"],
    "試算表": ["xlsx"],
    # Action synonyms
    "brainstorm": ["brainstorming"],
    "debug": ["systematic-debugging", "four-step-debug"],
    "search": ["smart-search"],
    "搜尋": ["smart-search"],
    "搜索": ["smart-search"],
    "查": ["smart-search"],
    "排程": ["scheduler"],
    "排班": ["scheduler"],
    "cron": ["scheduler"],
    "cronicle": ["scheduler"],
    "記住": ["memvault"],
    "記得": ["memvault"],
    "diagram": ["diagram-gen"],
    "flowchart": ["diagram-gen"],
    "mermaid": ["diagram-gen"],
    # Frontend
    "landing": ["frontend-design"],
    "page": ["frontend-design"],
    "website": ["frontend-design"],
    "網頁": ["frontend-design"],
    "前端": ["frontend-design"],
    # MCP
    "mcp": ["mcp-builder"],
    # Video
    "剪": ["video-edit"],
    "剪輯": ["video-edit"],
    "影片": ["video-edit", "video-core"],
    "video": ["video-edit", "video-core"],
    # readme
    "readme": ["readme-gen"],
}

# ── Intent signals: Chinese/English action phrases → skill boosts ──
# Each entry: (pattern, [skill_names], boost_score)
# Patterns are checked as substrings against the full query.
INTENT_SIGNALS = [
    # Writing/content
    ("寫文章", ["content-writer"], 6),
    ("撰寫", ["content-writer"], 6),
    ("寫一篇", ["content-writer"], 6),
    ("draft", ["content-writer"], 4),
    ("write an article", ["content-writer"], 6),
    ("blog post", ["content-writer"], 6),
    ("文章", ["content-writer"], 4),
    # Presentation
    ("做簡報", ["pptx"], 6),
    ("做ppt", ["pptx"], 8),
    ("make slides", ["pptx"], 6),
    ("做投影片", ["pptx"], 6),
    # Debugging/monitoring
    ("壞了", ["systematic-debugging", "sentinel"], 6),
    ("出錯", ["systematic-debugging", "four-step-debug"], 6),
    ("error", ["systematic-debugging", "four-step-debug"], 4),
    ("health check", ["sentinel"], 6),
    ("服務狀態", ["sentinel"], 6),
    ("掛了", ["sentinel", "systematic-debugging"], 6),
    # Search
    ("查一下", ["smart-search"], 6),
    ("幫我找", ["smart-search"], 6),
    ("搜一下", ["smart-search"], 6),
    ("look up", ["smart-search"], 4),
    ("research", ["smart-search"], 4),
    # Memory
    ("以後都", ["memvault"], 4),
    ("永遠", ["memvault"], 4),
    ("always use", ["memvault"], 4),
    ("never use", ["memvault"], 4),
    # Scheduling
    ("每天", ["scheduler"], 4),
    ("定時", ["scheduler"], 6),
    ("定期", ["scheduler"], 6),
    ("every day", ["scheduler"], 4),
    ("every hour", ["scheduler"], 4),
    # Skill management
    ("optimize", ["skill-optimizer"], 6),
    ("優化 skill", ["skill-optimizer"], 6),
    ("improve skill", ["skill-optimizer"], 6),
    ("audit skill", ["skill-optimizer"], 4),
    # Brainstorming (boost to compete with design skills)
    ("brainstorm", ["brainstorming"], 8),
    ("腦力激盪", ["brainstorming"], 6),
    ("想一下", ["brainstorming"], 4),
    ("想想", ["brainstorming"], 4),
    # Monitoring (boost sentinel for infra keywords)
    ("health", ["sentinel"], 4),
    ("monitor", ["sentinel"], 4),
    ("deploy", ["sentinel"], 3),
    ("uptime", ["sentinel"], 4),
    ("服務", ["sentinel"], 3),
    # Frontend / landing page
    ("landing page", ["frontend-design"], 8),
    ("做網頁", ["frontend-design"], 6),
    ("做網站", ["frontend-design"], 6),
    ("build a website", ["frontend-design"], 6),
    ("dashboard", ["frontend-design"], 4),
    # MCP server building
    ("mcp server", ["mcp-builder"], 8),
    ("mcp tool", ["mcp-builder"], 6),
    ("寫個mcp", ["mcp-builder"], 8),
    ("build mcp", ["mcp-builder"], 6),
    # README
    ("readme", ["readme-gen"], 8),
    ("寫readme", ["readme-gen"], 8),
    ("write readme", ["readme-gen"], 8),
    ("generate readme", ["readme-gen"], 6),
    # Video editing
    ("剪影片", ["video-edit"], 6),
    ("剪一下", ["video-edit"], 6),
    ("edit video", ["video-edit"], 6),
    ("trim video", ["video-edit"], 6),
    ("影片剪", ["video-edit"], 6),
]

# ── CJK noise: common words that pollute matching ──
CJK_NOISE = {"幫我", "一下", "這個", "那個", "可以", "請", "我想", "我要", "幫", "我"}


def load_index() -> list[dict]:
    if not TRIGGERS_PATH.exists():
        print(f"Error: {TRIGGERS_PATH} not found", file=sys.stderr)
        sys.exit(1)
    return json.loads(TRIGGERS_PATH.read_text())


def load_hot_skills() -> list[str]:
    if HOT_SKILLS_PATH.exists():
        return json.loads(HOT_SKILLS_PATH.read_text())
    return []


CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def tokenize(text: str) -> list[str]:
    """Tokenize text: split English on spaces, split CJK into chars + bigrams."""
    lower = text.lower()
    tokens = []
    for w in re.split(r"[^a-z0-9\u4e00-\u9fff\u3400-\u4dbf]+", lower):
        if not w:
            continue
        cjk_chars = [c for c in w if CJK_RE.match(c)]
        if cjk_chars:
            tokens.extend(cjk_chars)
            for i in range(len(cjk_chars) - 1):
                tokens.append(cjk_chars[i] + cjk_chars[i + 1])
        else:
            tokens.append(w)
    return tokens


def expand_aliases(tokens: list[str], query: str) -> list[str]:
    """Expand query tokens with alias mappings."""
    expanded = list(tokens)
    query_lower = query.lower()
    for alias_key, alias_values in ALIASES.items():
        if alias_key in tokens or alias_key in query_lower:
            for v in alias_values:
                if v not in expanded:
                    expanded.append(v)
    return expanded


def strip_noise(tokens: list[str]) -> list[str]:
    """Remove CJK noise tokens that pollute scoring."""
    return [t for t in tokens if t not in CJK_NOISE]


def extract_cjk(text: str) -> str:
    return "".join(CJK_RE.findall(text))


def cjk_overlap_score(query_cjk: str, target_cjk: str) -> float:
    if not query_cjk or not target_cjk:
        return 0.0
    q_set = set(query_cjk)
    t_set = set(target_cjk)
    intersection = q_set & t_set
    if not intersection:
        return 0.0
    return len(intersection) / min(len(q_set), len(t_set))


def score_skill(skill: dict, query: str, query_tokens: list[str]) -> float:
    query_lower = query.lower().strip()
    score = 0.0

    name = skill.get("name", "")
    description = skill.get("description", "")
    triggers = skill.get("triggers", [])
    domain = skill.get("domain", "")
    tags = skill.get("tags", [])

    name_lower = name.lower()
    desc_lower = description.lower()

    # Expand aliases and strip noise
    tokens = expand_aliases(query_tokens, query)
    clean_tokens = strip_noise(tokens)

    # 1. Exact trigger phrase match (strongest signal)
    for trigger in triggers:
        tl = trigger.lower()
        if tl == query_lower:
            score += 10
        elif tl in query_lower or query_lower in tl:
            score += 5

    # 2. Name match
    if name_lower == query_lower or name_lower in query_lower:
        score += 8
    elif name_lower in tokens:
        # Alias-expanded token matches name exactly (e.g., "pptx" from "ppt")
        score += 8
    elif any(t == name_lower for t in tokens):
        score += 6

    # 3. Name component match (split by "-")
    # "diagram" matches "diagram-gen", "brainstorm" prefix-matches "brainstorming"
    name_parts = name_lower.replace("-", " ").split()
    for token in clean_tokens:
        if len(token) >= 3:
            for part in name_parts:
                if part == token:
                    score += 5
                elif len(token) >= 4 and (
                    part.startswith(token) or token.startswith(part)
                ):
                    score += 3

    # 4. Description keyword match
    for token in clean_tokens:
        if len(token) >= 2 and token in desc_lower:
            score += 2

    # 5. Trigger keyword match (partial)
    trigger_text = " ".join(t.lower() for t in triggers)
    for token in clean_tokens:
        if len(token) >= 2 and token in trigger_text:
            score += 1.5

    # 6. Prefix/stem match on trigger words
    # "brainstorm" matches trigger word "brainstorming"
    trigger_words = set(trigger_text.split())
    for token in clean_tokens:
        if len(token) >= 4:
            for tw in trigger_words:
                if tw != token and (tw.startswith(token) or token.startswith(tw)):
                    score += 3
                    break  # one match per token

    # 7. Domain match
    if domain and any(t == domain.lower() for t in clean_tokens):
        score += 3

    # 8. Tag match
    for tag in tags:
        if tag.lower() in clean_tokens:
            score += 2

    # 9. Intent signals (phrase-level matching)
    for pattern, target_skills, boost in INTENT_SIGNALS:
        if pattern in query_lower and name_lower in target_skills:
            score += boost

    # 10. CJK character overlap
    query_cjk = extract_cjk(query_lower)
    # Strip noise characters from CJK query
    noise_chars = set()
    for noise in CJK_NOISE:
        noise_chars.update(noise)
    clean_query_cjk = "".join(c for c in query_cjk if c not in noise_chars)

    if clean_query_cjk:
        for trigger in triggers:
            trigger_cjk = extract_cjk(trigger.lower())
            overlap = cjk_overlap_score(clean_query_cjk, trigger_cjk)
            if overlap >= 0.5:
                score += overlap * 6
        desc_cjk = extract_cjk(desc_lower)
        overlap = cjk_overlap_score(clean_query_cjk, desc_cjk)
        if overlap >= 0.3:
            score += overlap * 3

    return round(score, 1)


def match(query: str, top_n: int = 3, cold_only: bool = False) -> list[dict]:
    index = load_index()
    hot_skills = load_hot_skills()
    tokens = tokenize(query)

    results = []
    for skill in index:
        if cold_only and skill["name"] in hot_skills:
            continue
        s = score_skill(skill, query, tokens)
        if s > 0:
            results.append(
                {
                    "name": skill["name"],
                    "score": s,
                    "description": skill.get("description", "")[:150],
                    "is_hot": skill["name"] in hot_skills,
                }
            )

    results.sort(key=lambda x: -x["score"])
    return results[:top_n]


def main():
    args = sys.argv[1:]

    if not args or "--help" in args:
        print(__doc__)
        sys.exit(0)

    if "--hot" in args:
        hot = load_hot_skills()
        print(f"Hot skills ({len(hot)}):")
        for s in hot:
            print(f"  {s}")
        return

    if "--stats" in args:
        index = load_index()
        hot = load_hot_skills()
        total_skills = len(list((Path.home() / ".claude/skills").iterdir()))
        indexed = len(index)
        cold = indexed - len([s for s in index if s["name"] in hot])
        print(f"Total skills:   {total_skills}")
        print(f"Indexed:        {indexed}")
        print(f"Hot (full desc): {len(hot)}")
        print(f"Cold (proxy):    {cold}")
        print(f"Not indexed:    {total_skills - indexed}")
        return

    top_n = 3
    if "--top" in args:
        idx = args.index("--top")
        top_n = int(args[idx + 1])
        args = [a for i, a in enumerate(args) if i != idx and i != idx + 1]

    query = " ".join(a for a in args if not a.startswith("--"))
    if not query:
        print("Error: no query provided", file=sys.stderr)
        sys.exit(1)

    results = match(query, top_n=top_n)

    if not results:
        print(json.dumps({"matches": [], "query": query}))
        return

    output = {
        "query": query,
        "matches": results,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
