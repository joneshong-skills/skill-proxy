#!/usr/bin/env python3
"""
Build embedding cache for skill-proxy semantic matching.

Reads triggers.json, computes embeddings for each skill's combined text
(name + description + triggers), and saves to embeddings-cache.json.

Usage:
    build_embeddings.py                     # build with qwen3-embedding:0.6b
    build_embeddings.py --model nomic-embed-text  # use different model
    build_embeddings.py --check             # verify cache freshness
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

TRIGGERS_PATH = Path.home() / ".claude/data/skill-index/triggers.json"
CACHE_PATH = Path.home() / ".claude/data/skill-index/embeddings-cache.json"
DEFAULT_MODEL = "qwen3-embedding:0.6b"
OLLAMA_URL = "http://localhost:11434/api/embed"
BATCH_SIZE = 10  # embed N skills per API call


def embed_batch(texts: list[str], model: str = DEFAULT_MODEL) -> list[list[float]]:
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())["embeddings"]


def skill_text(skill: dict) -> str:
    """Combine skill fields into a single text for embedding."""
    parts = [skill.get("name", "")]
    desc = skill.get("description", "")
    if desc:
        parts.append(desc[:300])
    triggers = skill.get("triggers", [])
    if triggers:
        parts.append(" ".join(triggers[:10]))
    return " ".join(parts)


def build_cache(model: str = DEFAULT_MODEL) -> dict:
    if not TRIGGERS_PATH.exists():
        print(f"Error: {TRIGGERS_PATH} not found", file=sys.stderr)
        sys.exit(1)

    index = json.loads(TRIGGERS_PATH.read_text())
    print(f"Building embeddings for {len(index)} skills with {model}...")

    cache = {"model": model, "version": 1, "skills": {}}
    texts = []
    names = []

    for skill in index:
        name = skill.get("name", "")
        text = skill_text(skill)
        names.append(name)
        texts.append(text)

    # Process in batches
    t0 = time.time()
    for i in range(0, len(texts), BATCH_SIZE):
        batch_texts = texts[i : i + BATCH_SIZE]
        batch_names = names[i : i + BATCH_SIZE]
        embeddings = embed_batch(batch_texts, model)
        for name, emb in zip(batch_names, embeddings):
            cache["skills"][name] = emb
        done = min(i + BATCH_SIZE, len(texts))
        print(f"  {done}/{len(texts)} embedded", end="\r")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s — {len(cache['skills'])} skills cached")

    # Save
    CACHE_PATH.write_text(json.dumps(cache))
    size_mb = CACHE_PATH.stat().st_size / 1024 / 1024
    print(f"Saved to {CACHE_PATH} ({size_mb:.1f} MB)")
    return cache


def check_freshness():
    if not CACHE_PATH.exists():
        print("No cache found. Run build_embeddings.py to create.")
        return False

    cache = json.loads(CACHE_PATH.read_text())
    index = json.loads(TRIGGERS_PATH.read_text())
    cached_names = set(cache.get("skills", {}).keys())
    index_names = {s["name"] for s in index}
    missing = index_names - cached_names
    extra = cached_names - index_names

    print(f"Cache: {len(cached_names)} skills, model={cache.get('model')}")
    print(f"Index: {len(index_names)} skills")
    if missing:
        print(f"Missing from cache: {missing}")
    if extra:
        print(f"Extra in cache: {extra}")
    if not missing and not extra:
        print("Cache is up to date.")
        return True
    return False


def main():
    args = sys.argv[1:]
    model = DEFAULT_MODEL

    if "--check" in args:
        check_freshness()
        return

    if "--model" in args:
        idx = args.index("--model")
        model = args[idx + 1]

    build_cache(model)


if __name__ == "__main__":
    main()
