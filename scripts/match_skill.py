#!/usr/bin/env python3
"""
Skill Proxy — hybrid BM25 + embedding skill matching with score-weighted fusion.

Architecture (adapted from workshop/qdrant hybrid search):
  Phase 1: BM25 keyword scoring (aliases, intent signals, CJK bigrams)
  Phase 2: Embedding similarity (Qwen3-Embedding-0.6B via oMLX bridge)
  Phase 3: Score-weighted fusion — normalized BM25 + embedding sim with dynamic alpha

Usage:
    match_skill.py "user query here"
    match_skill.py --top 5 "create a diagram"
    match_skill.py --no-embed "query"     # BM25 only (skip oMLX)
    match_skill.py --hot                  # list current hot skills
    match_skill.py --stats                # show hot/cold breakdown

BM25 scoring layers:
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
  13. Intent signals:               +6~10 per match

Embedding layer:
  14. Cosine similarity via pre-computed cache + live query embedding
  Score-weighted fusion combines normalized BM25 + embedding similarity.
  Dynamic alpha: high BM25 confidence → trust BM25 more; low → lean on embedding.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

TRIGGERS_PATH = Path.home() / ".claude/data/skill-index/triggers.json"
HOT_SKILLS_PATH = Path.home() / ".claude/data/skill-index/hot-skills.json"
EMBEDDINGS_CACHE_PATH = Path.home() / ".claude/data/skill-index/embeddings-cache.json"

# oMLX embedding bridge (Qwen3-Embedding-0.6B via persistent subprocess)
OMLX_PYTHON = Path.home() / ".venvs/omlx/bin/python3"
OMLX_WORKER = Path.home() / ".venvs/omlx/embed_worker.py"

# Embedding config
EMBED_THRESHOLD = 0.45  # minimum cosine similarity to contribute

# Score-weighted fusion config (adapted from workshop/qdrant RRF pattern)
# Dynamic alpha: high BM25 confidence → trust BM25 more; low → lean on embedding
FUSION_ALPHA_HIGH = 0.8  # alpha when max_bm25 >= 20 (strong intent signal match)
FUSION_ALPHA_MED = 0.6  # alpha when max_bm25 >= 10
FUSION_ALPHA_LOW = 0.4  # alpha when max_bm25 < 10 (ambiguous, rely on embedding)

# ── Alias expansion: abbreviations/synonyms → canonical skill names ──
ALIASES = {
    # Format abbreviations
    "ppt": ["pptx"],
    "簡報": ["pptx", "presentation"],
    "投影片": ["pptx"],
    "slide": ["pptx"],
    "slides": ["pptx"],
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
    # Document formats
    "word": ["docx"],
    "excel": ["xlsx"],
    "報表": ["xlsx"],
    # Video
    "剪": ["video-edit"],
    "剪輯": ["video-edit"],
    "影片": ["video-edit", "video-core"],
    "video": ["video-edit", "video-core"],
    # readme
    "readme": ["readme-gen"],
    # GitHub
    "issue": ["github-pm"],
    "github": ["github-pm"],
    # Screen recording
    "錄": ["screen-record"],
    "錄影": ["screen-record"],
    "錄製": ["screen-record"],
}

# ── Intent signals: Chinese/English action phrases → skill boosts ──
# Each entry: (pattern, [skill_names], boost_score)
# Patterns are checked as substrings against the full query (case-insensitive).
INTENT_SIGNALS = [
    # Writing/content
    ("寫文章", ["content-writer"], 6),
    ("撰寫", ["content-writer"], 6),
    ("寫一篇", ["content-writer"], 6),
    ("draft", ["content-writer"], 4),
    ("write an article", ["content-writer"], 6),
    ("blog post", ["content-writer"], 6),
    ("文章", ["content-writer"], 4),
    ("行銷郵件", ["content-writer"], 8),
    ("行銷信", ["content-writer"], 6),
    ("marketing email", ["content-writer"], 6),
    # Presentation
    ("做簡報", ["pptx"], 6),
    ("做ppt", ["pptx"], 8),
    ("make slides", ["pptx"], 6),
    ("做投影片", ["pptx"], 8),
    ("投影片報告", ["pptx"], 8),
    ("投影片", ["pptx"], 6),
    # Debugging/monitoring
    ("壞了", ["systematic-debugging", "sentinel"], 6),
    ("出錯", ["systematic-debugging", "four-step-debug"], 6),
    ("error", ["systematic-debugging", "four-step-debug"], 4),
    ("health check", ["sentinel"], 6),
    ("服務狀態", ["sentinel"], 6),
    ("掛了", ["sentinel", "systematic-debugging"], 6),
    ("有bug", ["systematic-debugging", "four-step-debug"], 8),
    ("有 bug", ["systematic-debugging", "four-step-debug"], 8),
    ("程式碼有", ["systematic-debugging"], 4),
    ("bug", ["systematic-debugging", "four-step-debug"], 4),
    # Search
    ("查一下", ["smart-search"], 6),
    ("幫我找", ["smart-search"], 6),
    ("搜一下", ["smart-search"], 6),
    ("look up", ["smart-search"], 4),
    ("research", ["smart-search"], 4),
    ("想知道", ["smart-search"], 6),
    ("怎麼用", ["smart-search"], 6),
    ("claude api", ["smart-search"], 12),
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
    ("每小時", ["scheduler"], 6),
    ("cron job", ["scheduler"], 8),
    ("cron", ["scheduler"], 4),
    # Skill management
    ("optimize", ["skill-optimizer"], 6),
    ("優化 skill", ["skill-optimizer"], 6),
    ("improve skill", ["skill-optimizer"], 6),
    ("audit skill", ["skill-optimizer"], 4),
    # Brainstorming
    ("brainstorm", ["brainstorming"], 8),
    ("腦力激盪", ["brainstorming"], 6),
    ("想一下", ["brainstorming"], 4),
    ("想想", ["brainstorming"], 4),
    # Monitoring
    ("health", ["sentinel"], 4),
    ("monitor", ["sentinel"], 4),
    ("deploy", ["sentinel"], 3),
    ("uptime", ["sentinel"], 4),
    ("服務", ["sentinel"], 3),
    ("系統效能", ["system-monitor"], 8),
    ("效能", ["system-monitor"], 4),
    ("system performance", ["system-monitor"], 6),
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
    # STT / TTS direction-specific (critical: prevents stt↔tts confusion)
    # High boosts needed to overcome shared CJK token overlap between stt/tts
    ("音檔轉成文字", ["stt"], 15),
    ("轉成文字", ["stt"], 12),
    ("轉文字", ["stt"], 10),
    ("語音轉文字", ["stt"], 15),
    ("音轉文", ["stt"], 10),
    ("speech to text", ["stt"], 10),
    ("transcribe", ["stt"], 8),
    ("transcription", ["stt"], 8),
    ("文字轉語音", ["tts"], 15),
    ("轉成語音", ["tts"], 12),
    ("轉語音", ["tts"], 10),
    ("文轉音", ["tts"], 10),
    ("text to speech", ["tts"], 10),
    # Docs / API docs
    ("api文件", ["docs-butler"], 8),
    ("文件更新", ["docs-butler"], 6),
    ("文件太舊", ["docs-butler"], 8),
    ("更新文件", ["docs-butler"], 6),
    ("update docs", ["docs-butler"], 6),
    ("update documentation", ["docs-butler"], 6),
    # Quote building
    ("做報價", ["quote-builder"], 8),
    ("做個報價", ["quote-builder"], 8),
    ("報價單", ["quote-builder"], 8),
    ("build quote", ["quote-builder"], 6),
    ("make a quote", ["quote-builder"], 6),
    # GitHub PM
    ("開issue", ["github-pm"], 8),
    ("開 issue", ["github-pm"], 8),
    ("github issue", ["github-pm"], 8),
    ("new issue", ["github-pm"], 6),
    ("建立issue", ["github-pm"], 6),
    # Screen recording
    ("錄影", ["screen-record"], 6),
    ("錄操作", ["screen-record"], 8),
    ("示範影片", ["screen-record"], 8),
    ("操作示範", ["screen-record"], 8),
    ("screen record", ["screen-record"], 6),
    ("錄一段", ["screen-record"], 6),
    # Social content / image gen
    ("社群媒體", ["social-content"], 8),
    ("social media", ["social-content"], 8),
    ("社群圖片", ["social-content"], 10),
    ("社群媒體圖片", ["social-content"], 12),
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


# ── Embedding helpers (oMLX subprocess bridge) ──

_embed_cache: dict | None = None
_omlx_proc: subprocess.Popen | None = None


def _ensure_omlx() -> bool:
    """Start oMLX worker subprocess if not running."""
    global _omlx_proc
    if _omlx_proc is not None and _omlx_proc.poll() is None:
        return True
    if not OMLX_PYTHON.exists() or not OMLX_WORKER.exists():
        return False
    try:
        _omlx_proc = subprocess.Popen(
            [str(OMLX_PYTHON), str(OMLX_WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        line = _omlx_proc.stdout.readline()
        if not line:
            _omlx_proc.kill()
            _omlx_proc = None
            return False
        status = json.loads(line.strip())
        return status.get("status") == "ready"
    except Exception:
        if _omlx_proc:
            try:
                _omlx_proc.kill()
            except ProcessLookupError:
                pass
        _omlx_proc = None
        return False


def load_embeddings() -> dict[str, list[float]]:
    """Load pre-computed skill embeddings from cache."""
    global _embed_cache
    if _embed_cache is not None:
        return _embed_cache
    if EMBEDDINGS_CACHE_PATH.exists():
        data = json.loads(EMBEDDINGS_CACHE_PATH.read_text())
        _embed_cache = data.get("skills", {})
    else:
        _embed_cache = {}
    return _embed_cache


def embed_query(text: str) -> list[float] | None:
    """Embed query text via oMLX bridge. Returns None on failure."""
    if not _ensure_omlx():
        return None
    try:
        req = {"texts": [text], "task_type": "search_query"}
        _omlx_proc.stdin.write(json.dumps(req) + "\n")
        _omlx_proc.stdin.flush()
        line = _omlx_proc.stdout.readline()
        if not line:
            return None
        resp = json.loads(line.strip())
        if "error" in resp:
            return None
        embeddings = resp.get("embeddings", [])
        return embeddings[0] if embeddings else None
    except Exception:
        return None


def cosine_sim(a: list[float], b: list[float]) -> float:
    """Fast cosine similarity."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ── Score-weighted Fusion (adapted from workshop/qdrant RRF pattern) ──


def compute_alpha(max_bm25: float) -> float:
    """Dynamic alpha based on BM25 confidence.

    High BM25 max → strong intent signal match → trust BM25 more.
    Low BM25 max → ambiguous query → lean on embedding semantics.
    """
    if max_bm25 >= 20:
        return FUSION_ALPHA_HIGH  # 0.8
    elif max_bm25 >= 10:
        return FUSION_ALPHA_MED  # 0.6
    else:
        return FUSION_ALPHA_LOW  # 0.4


def score_fuse(
    bm25_ranked: list[tuple[str, float]],
    embed_ranked: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Score-weighted fusion — normalize BM25 scores, combine with embedding similarity.

    final(d) = alpha * norm_bm25(d) + (1-alpha) * embed_sim(d)
    where alpha is dynamically chosen based on BM25 confidence.
    """
    if not bm25_ranked and not embed_ranked:
        return []

    max_bm25 = bm25_ranked[0][1] if bm25_ranked else 1.0
    alpha = compute_alpha(max_bm25)

    bm25_map = {name: score for name, score in bm25_ranked}
    embed_map = {name: sim for name, sim in embed_ranked}

    all_names = set(bm25_map.keys()) | set(embed_map.keys())
    fused = []
    for name in all_names:
        bm25_norm = bm25_map.get(name, 0) / max_bm25 if max_bm25 > 0 else 0
        embed_sim = embed_map.get(name, 0)
        score = alpha * bm25_norm + (1 - alpha) * embed_sim
        fused.append((name, score))

    fused.sort(key=lambda x: -x[1])
    return fused


# ── Tokenizer ──

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def tokenize(text: str) -> list[str]:
    """Tokenize text: split English on spaces, split CJK into chars + bigrams.

    Handles mixed CJK+English words (e.g. "幫我做一個diagram") by extracting
    both CJK chars/bigrams AND English substrings from the same token.
    """
    lower = text.lower()
    tokens = []
    for w in re.split(r"[^a-z0-9\u4e00-\u9fff\u3400-\u4dbf]+", lower):
        if not w:
            continue
        # Split mixed word into CJK runs and English runs
        cjk_chars = []
        eng_buf = []
        for c in w:
            if CJK_RE.match(c):
                if eng_buf:
                    eng = "".join(eng_buf)
                    if eng:
                        tokens.append(eng)
                    eng_buf = []
                cjk_chars.append(c)
            else:
                eng_buf.append(c)
        if eng_buf:
            eng = "".join(eng_buf)
            if eng:
                tokens.append(eng)
        if cjk_chars:
            tokens.extend(cjk_chars)
            for i in range(len(cjk_chars) - 1):
                tokens.append(cjk_chars[i] + cjk_chars[i + 1])
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


# ── BM25 scoring ──


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
        score += 8
    elif any(t == name_lower for t in tokens):
        score += 6

    # 3. Name component match (split by "-")
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
    trigger_words = set(trigger_text.split())
    for token in clean_tokens:
        if len(token) >= 4:
            for tw in trigger_words:
                if tw != token and (tw.startswith(token) or token.startswith(tw)):
                    score += 3
                    break

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


# ── Main matching engine ──


def match(
    query: str, top_n: int = 3, cold_only: bool = False, no_embed: bool = False
) -> list[dict]:
    """Hybrid BM25 + Embedding matching with RRF fusion."""
    index = load_index()
    hot_skills = load_hot_skills()
    tokens = tokenize(query)

    # Phase 1: BM25 scoring for all skills
    skill_map: dict[str, dict] = {}
    bm25_ranked: list[tuple[str, float]] = []
    for skill in index:
        name = skill["name"]
        if cold_only and name in hot_skills:
            continue
        score = score_skill(skill, query, tokens)
        skill_map[name] = skill
        if score > 0:
            bm25_ranked.append((name, score))
    bm25_ranked.sort(key=lambda x: -x[1])

    # Phase 2: Embedding scoring
    embed_ranked: list[tuple[str, float]] = []
    if not no_embed:
        skill_embeds = load_embeddings()
        if skill_embeds:
            q_vec = embed_query(query)
            if q_vec:
                for name, s_vec in skill_embeds.items():
                    if cold_only and name in hot_skills:
                        continue
                    sim = cosine_sim(q_vec, s_vec)
                    if sim >= EMBED_THRESHOLD:
                        embed_ranked.append((name, sim))
                embed_ranked.sort(key=lambda x: -x[1])

    # Phase 3: Score-weighted fusion or BM25-only fallback
    if embed_ranked:
        fused = score_fuse(bm25_ranked, embed_ranked)
        ranking = [name for name, _ in fused[:top_n]]
    else:
        ranking = [name for name, _ in bm25_ranked[:top_n]]

    # Build output with both scores for transparency
    bm25_map = dict(bm25_ranked)
    embed_map = dict(embed_ranked)
    results = []
    for name in ranking:
        skill = skill_map.get(name)
        if not skill:
            continue
        entry = {
            "name": name,
            "score": round(bm25_map.get(name, 0), 1),
            "description": skill.get("description", "")[:150],
            "is_hot": name in hot_skills,
        }
        if name in embed_map:
            entry["embed_sim"] = round(embed_map[name], 4)
        results.append(entry)

    return results


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

    no_embed = "--no-embed" in args

    query = " ".join(a for a in args if not a.startswith("--"))
    if not query:
        print("Error: no query provided", file=sys.stderr)
        sys.exit(1)

    results = match(query, top_n=top_n, no_embed=no_embed)

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
