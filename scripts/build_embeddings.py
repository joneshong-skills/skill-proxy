#!/usr/bin/env python3
"""
Build embedding cache for skill-proxy semantic matching.

Reads triggers.json, computes embeddings for each skill's combined text
(name + description + triggers), and saves to embeddings-cache.json.

Uses oMLX bridge (Qwen3-Embedding-0.6B via persistent subprocess worker).

Usage:
    build_embeddings.py                     # build cache
    build_embeddings.py --check             # verify cache freshness
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

TRIGGERS_PATH = Path.home() / ".claude/data/skill-index/triggers.json"
CACHE_PATH = Path.home() / ".claude/data/skill-index/embeddings-cache.json"
OMLX_PYTHON = Path.home() / ".venvs/omlx/bin/python3"
OMLX_WORKER = Path.home() / ".venvs/omlx/embed_worker.py"
BATCH_SIZE = 10  # embed N skills per request

_omlx_proc: subprocess.Popen | None = None


def _ensure_omlx() -> bool:
    """Start oMLX worker subprocess if not running."""
    global _omlx_proc
    if _omlx_proc is not None and _omlx_proc.poll() is None:
        return True
    if not OMLX_PYTHON.exists() or not OMLX_WORKER.exists():
        print(f"Error: oMLX venv not found at {OMLX_PYTHON}", file=sys.stderr)
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
        if status.get("status") == "ready":
            print(f"oMLX worker ready: {status.get('model')}")
            return True
        _omlx_proc.kill()
        _omlx_proc = None
        return False
    except Exception as e:
        print(f"Failed to start oMLX worker: {e}", file=sys.stderr)
        if _omlx_proc:
            try:
                _omlx_proc.kill()
            except ProcessLookupError:
                pass
        _omlx_proc = None
        return False


def _shutdown_omlx():
    """Gracefully shutdown the worker."""
    global _omlx_proc
    if _omlx_proc and _omlx_proc.poll() is None:
        try:
            _omlx_proc.stdin.close()
            _omlx_proc.wait(timeout=5)
        except Exception:
            _omlx_proc.kill()
    _omlx_proc = None


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via oMLX bridge."""
    if not _ensure_omlx():
        raise RuntimeError("oMLX worker not available")
    req = {"texts": texts, "task_type": "search_document"}
    _omlx_proc.stdin.write(json.dumps(req) + "\n")
    _omlx_proc.stdin.flush()
    line = _omlx_proc.stdout.readline()
    if not line:
        raise RuntimeError("oMLX worker returned empty response")
    resp = json.loads(line.strip())
    if "error" in resp:
        raise RuntimeError(f"oMLX embedding error: {resp['error']}")
    return resp.get("embeddings", [])


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


def build_cache() -> dict:
    if not TRIGGERS_PATH.exists():
        print(f"Error: {TRIGGERS_PATH} not found", file=sys.stderr)
        sys.exit(1)

    index = json.loads(TRIGGERS_PATH.read_text())
    print(f"Building embeddings for {len(index)} skills via oMLX...")

    cache = {"model": "Qwen3-Embedding-0.6B-oMLX", "version": 2, "skills": {}}
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
        embeddings = embed_batch(batch_texts)
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

    _shutdown_omlx()
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

    if "--check" in args:
        check_freshness()
        return

    build_cache()


if __name__ == "__main__":
    main()
