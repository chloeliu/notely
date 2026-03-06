"""Todo dedup — pure functions for detecting and merging duplicate action items."""

import re
import string
from difflib import SequenceMatcher
from typing import Any

DEDUP_SIMILARITY_THRESHOLD = 0.80

_FILLER_WORDS = frozenset(
    {"the", "a", "an", "to", "for", "with", "and", "or", "of", "in", "on", "is", "be"}
)


def normalize_task(task: str) -> str:
    """Lowercase, strip punctuation, remove filler words, collapse whitespace."""
    text = task.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    words = text.split()
    # Only strip filler words when enough words remain to avoid false positives
    if len(words) > 3:
        words = [w for w in words if w not in _FILLER_WORDS]
    return " ".join(words)


def task_similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio on normalized task text."""
    return SequenceMatcher(None, normalize_task(a), normalize_task(b)).ratio()


def find_duplicate_clusters(
    items: list[dict[str, Any]],
    threshold: float = DEDUP_SIMILARITY_THRESHOLD,
) -> list[list[dict[str, Any]]]:
    """Group items into duplicate clusters using union-find.

    Groups by owner first — same task for different people is NOT a duplicate.
    Returns only clusters of 2+ items.
    """
    if len(items) < 2:
        return []

    # Group by normalized owner
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = (item.get("owner") or "").strip().lower()
        by_owner.setdefault(key, []).append(item)

    # Union-find
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    # Initialize parent for every item
    for item in items:
        parent[item["id"]] = item["id"]

    # Pairwise compare within each owner group
    for group in by_owner.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if task_similarity(group[i]["task"], group[j]["task"]) >= threshold:
                    union(group[i]["id"], group[j]["id"])

    # Collect clusters
    clusters_map: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        root = find(item["id"])
        clusters_map.setdefault(root, []).append(item)

    return [c for c in clusters_map.values() if len(c) >= 2]


def pick_best_task(cluster: list[dict[str, Any]]) -> str:
    """Pick the longest task string as the best representative."""
    return max(cluster, key=lambda x: len(x.get("task", "")))["task"]


def pick_earliest_due(cluster: list[dict[str, Any]]) -> str | None:
    """Pick the earliest non-null due date."""
    dues = [item["due"] for item in cluster if item.get("due")]
    return min(dues) if dues else None


def build_source_refs(cluster: list[dict[str, Any]]) -> str:
    """Build 'from: Title A, Title B' string from cluster note titles."""
    titles = []
    for item in cluster:
        title = item.get("note_title", "")
        if title and title != "(standalone)":
            titles.append(title)
    if not titles:
        return ""
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return "from: " + ", ".join(unique)
