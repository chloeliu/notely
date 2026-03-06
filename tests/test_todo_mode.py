"""Tests for todo mode — DB methods and timer integration."""

from __future__ import annotations

from notely.db import Database


def test_flag_today(db: Database):
    """Test flagging and unflagging action items."""
    item_id = db.add_standalone_action_item(
        owner="Chloe", task="Test task", space="test"
    )

    # Initially no flag
    item = db.get_action_item(item_id)
    assert item["flagged_date"] is None

    # Flag
    db.flag_today(item_id, "2026-03-05")
    item = db.get_action_item(item_id)
    assert item["flagged_date"] == "2026-03-05"

    # Unflag
    db.unflag_today(item_id)
    item = db.get_action_item(item_id)
    assert item["flagged_date"] is None


def test_get_open_action_items_includes_flagged_date(db: Database):
    """Verify get_open_action_items returns flagged_date and file_path."""
    item_id = db.add_standalone_action_item(
        owner="Chloe", task="Test task", space="test"
    )
    db.flag_today(item_id, "2026-03-05")

    items = db.get_open_action_items()
    assert len(items) >= 1

    found = [i for i in items if i["id"] == item_id]
    assert len(found) == 1
    assert found[0]["flagged_date"] == "2026-03-05"
    assert "file_path" in found[0]


def test_get_folder_for_item_standalone(db: Database):
    """Test folder resolution for standalone items."""
    item_id = db.add_standalone_action_item(
        owner="Chloe", task="Test", space="clients", group_name="sanity"
    )
    result = db.get_folder_for_item(item_id)
    assert result is not None
    space, group, display = result
    assert space == "clients"
    assert group == "sanity"


def test_get_folder_for_item_nonexistent(db: Database):
    """Test folder resolution for non-existent item."""
    assert db.get_folder_for_item(99999) is None


def test_timer_todo_id(config, tmp_workspace):
    """Test that timer entries can carry a todo_id."""
    from notely.timer import start_timer, stop_timer, get_time_for_todo, get_running_timer_for_todo

    entry = start_timer(config, "clients/sanity", "Test task", todo_id=42)
    assert entry["todo_id"] == "42"

    # Running timer should be found
    running = get_running_timer_for_todo(config, 42)
    assert running is not None
    assert running["todo_id"] == "42"

    # Stop it
    stop_timer(config, entry["id"])

    # No running timer now
    assert get_running_timer_for_todo(config, 42) is None

    # Time should be tracked
    total = get_time_for_todo(config, 42)
    assert total >= 0


def test_timer_todo_id_empty(config, tmp_workspace):
    """Test that timer without todo_id works."""
    from notely.timer import start_timer, get_running_timer_for_todo

    entry = start_timer(config, "test", "No todo link")
    assert entry["todo_id"] == ""

    # Should not find a running timer for any todo
    assert get_running_timer_for_todo(config, 1) is None
