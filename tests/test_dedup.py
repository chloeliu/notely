"""Tests for todo dedup pure functions."""

import pytest

from notely.dedup import (
    DEDUP_SIMILARITY_THRESHOLD,
    build_source_refs,
    find_duplicate_clusters,
    normalize_task,
    pick_best_task,
    pick_earliest_due,
    task_similarity,
)


# --- normalize_task ---


def test_normalize_strips_punctuation():
    assert normalize_task("Complete KYC verification!") == "complete kyc verification"


def test_normalize_strips_filler_words():
    result = normalize_task("Complete the KYC verification for the account")
    assert "the" not in result.split()
    assert "for" not in result.split()
    assert "complete" in result.split()


def test_normalize_preserves_short_tasks():
    # Short tasks (<=3 words after split) keep filler words
    result = normalize_task("Do the thing")
    assert result == "do the thing"


def test_normalize_collapses_whitespace():
    # "of" is a filler word but only 3 words so filler stripping is skipped
    assert normalize_task("  lots   of   spaces  ") == "lots of spaces"


# --- task_similarity ---


def test_similarity_identical():
    assert task_similarity("Complete KYC verification", "Complete KYC verification") == 1.0


def test_similarity_near_match():
    a = "Complete KYC identity verification via email link"
    b = "Complete the KYC verification via the email link"
    assert task_similarity(a, b) >= DEDUP_SIMILARITY_THRESHOLD


def test_similarity_different():
    a = "Complete KYC verification"
    b = "Schedule team standup meeting"
    assert task_similarity(a, b) < DEDUP_SIMILARITY_THRESHOLD


# --- find_duplicate_clusters ---


def _item(id, owner, task, due=None, note_title="Note"):
    return {
        "id": id,
        "owner": owner,
        "task": task,
        "due": due,
        "note_title": note_title,
    }


def test_clusters_basic():
    items = [
        _item(1, "Alice", "Complete KYC verification via email", note_title="Meeting A"),
        _item(2, "Alice", "Complete the KYC verification via email link", note_title="Meeting B"),
        _item(3, "Bob", "Schedule standup"),
    ]
    clusters = find_duplicate_clusters(items)
    assert len(clusters) == 1
    assert {i["id"] for i in clusters[0]} == {1, 2}


def test_clusters_different_owners_excluded():
    items = [
        _item(1, "Alice", "Complete KYC verification"),
        _item(2, "Bob", "Complete KYC verification"),
    ]
    clusters = find_duplicate_clusters(items)
    assert len(clusters) == 0


def test_clusters_transitive():
    # A~B and B~C should form one cluster {A, B, C}
    items = [
        _item(1, "Alice", "Complete KYC identity verification via email link"),
        _item(2, "Alice", "Complete the KYC verification via the email link"),
        _item(3, "Alice", "Complete KYC verification via email"),
    ]
    clusters = find_duplicate_clusters(items)
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_clusters_no_duplicates():
    items = [
        _item(1, "Alice", "Complete KYC verification"),
        _item(2, "Alice", "Schedule standup meeting"),
        _item(3, "Alice", "Review pull request"),
    ]
    clusters = find_duplicate_clusters(items)
    assert len(clusters) == 0


def test_clusters_empty_list():
    assert find_duplicate_clusters([]) == []


def test_clusters_single_item():
    assert find_duplicate_clusters([_item(1, "Alice", "Do stuff")]) == []


# --- pick_best_task ---


def test_pick_best_task_longest():
    cluster = [
        _item(1, "Alice", "Complete KYC verification"),
        _item(2, "Alice", "Complete KYC identity verification via email link"),
    ]
    assert pick_best_task(cluster) == "Complete KYC identity verification via email link"


# --- pick_earliest_due ---


def test_pick_earliest_due():
    cluster = [
        _item(1, "Alice", "Task", due="2026-03-10"),
        _item(2, "Alice", "Task", due="2026-03-05"),
        _item(3, "Alice", "Task", due=None),
    ]
    assert pick_earliest_due(cluster) == "2026-03-05"


def test_pick_earliest_due_all_none():
    cluster = [
        _item(1, "Alice", "Task", due=None),
        _item(2, "Alice", "Task", due=None),
    ]
    assert pick_earliest_due(cluster) is None


# --- build_source_refs ---


def test_build_source_refs():
    cluster = [
        _item(1, "Alice", "Task", note_title="Meeting A"),
        _item(2, "Alice", "Task", note_title="Meeting B"),
    ]
    assert build_source_refs(cluster) == "from: Meeting A, Meeting B"


def test_build_source_refs_deduplicates():
    cluster = [
        _item(1, "Alice", "Task", note_title="Same Meeting"),
        _item(2, "Alice", "Task", note_title="Same Meeting"),
    ]
    assert build_source_refs(cluster) == "from: Same Meeting"


def test_build_source_refs_skips_standalone():
    cluster = [
        _item(1, "Alice", "Task", note_title="Meeting A"),
        _item(2, "Alice", "Task", note_title="(standalone)"),
    ]
    assert build_source_refs(cluster) == "from: Meeting A"


def test_build_source_refs_all_standalone():
    cluster = [
        _item(1, "Alice", "Task", note_title="(standalone)"),
        _item(2, "Alice", "Task", note_title="(standalone)"),
    ]
    assert build_source_refs(cluster) == ""
