"""Tests for notely.prompts — standardized interactive prompt patterns."""

from unittest.mock import MagicMock, patch

import pytest

from notely.prompts import (
    confirm_action,
    confirm_destructive,
    duplicate_found,
    no_changes_retry,
    pick_from_list,
)


# ---------------------------------------------------------------------------
# confirm_action
# ---------------------------------------------------------------------------

class TestConfirmAction:
    """Tests for confirm_action()."""

    def test_yes_returns_true(self):
        preview = MagicMock()
        with patch("notely.prompts.Prompt.ask", return_value="Y"):
            assert confirm_action(preview) is True
        preview.assert_called_once()

    def test_no_returns_false(self):
        preview = MagicMock()
        with patch("notely.prompts.Prompt.ask", return_value="n"):
            assert confirm_action(preview) is False

    def test_edit_loops_back(self):
        preview = MagicMock()
        edit_fn = MagicMock()
        answers = iter(["e", "Y"])
        with patch("notely.prompts.Prompt.ask", side_effect=answers):
            assert confirm_action(preview, edit_fn=edit_fn) is True
        assert preview.call_count == 2
        edit_fn.assert_called_once()

    def test_revise_loops_back(self):
        preview = MagicMock()
        revise_fn = MagicMock()
        answers = iter(["r", "Y"])
        with patch("notely.prompts.Prompt.ask", side_effect=answers):
            assert confirm_action(preview, revise_fn=revise_fn) is True
        revise_fn.assert_called_once()

    def test_drop_continues_if_items_remain(self):
        preview = MagicMock()
        drop_fn = MagicMock(return_value=True)  # items remain
        answers = iter(["d", "Y"])
        with patch("notely.prompts.Prompt.ask", side_effect=answers):
            assert confirm_action(preview, drop_fn=drop_fn) is True
        drop_fn.assert_called_once()

    def test_drop_cancels_if_all_dropped(self):
        preview = MagicMock()
        drop_fn = MagicMock(return_value=False)  # all dropped
        with patch("notely.prompts.Prompt.ask", return_value="d"):
            assert confirm_action(preview, drop_fn=drop_fn) is False

    def test_keyboard_interrupt_returns_false(self):
        preview = MagicMock()
        with patch("notely.prompts.Prompt.ask", side_effect=KeyboardInterrupt):
            assert confirm_action(preview) is False

    def test_verb_appears_in_prompt(self):
        """Verify the verb is used in the prompt string."""
        preview = MagicMock()
        with patch("notely.prompts.Prompt.ask", return_value="Y") as mock_ask:
            confirm_action(preview, verb="merge")
        prompt_str = mock_ask.call_args[0][0]
        assert "merge" in prompt_str

    def test_options_reflect_callbacks(self):
        """Only provided callbacks appear in the prompt."""
        preview = MagicMock()
        with patch("notely.prompts.Prompt.ask", return_value="Y") as mock_ask:
            confirm_action(preview)
        prompt_str = mock_ask.call_args[0][0]
        assert "e]dit" not in prompt_str
        assert "r]evise" not in prompt_str
        assert "d]rop" not in prompt_str

        with patch("notely.prompts.Prompt.ask", return_value="Y") as mock_ask:
            confirm_action(
                preview,
                edit_fn=MagicMock(),
                revise_fn=MagicMock(),
                drop_fn=MagicMock(return_value=True),
            )
        prompt_str = mock_ask.call_args[0][0]
        assert "e]dit" in prompt_str
        assert "r]evise" in prompt_str
        assert "d]rop" in prompt_str


# ---------------------------------------------------------------------------
# pick_from_list
# ---------------------------------------------------------------------------

class TestPickFromList:
    """Tests for pick_from_list()."""

    def test_pick_numbered_item(self):
        items = [("a", "Alpha"), ("b", "Beta")]
        with patch("notely.prompts.Prompt.ask", return_value="1"):
            assert pick_from_list(items) == "1"

    def test_pick_second_item(self):
        items = [("a", "Alpha"), ("b", "Beta")]
        with patch("notely.prompts.Prompt.ask", return_value="2"):
            assert pick_from_list(items) == "2"

    def test_pick_extra_letter(self):
        items = [("a", "Alpha")]
        extras = [("n", "New"), ("s", "Skip")]
        with patch("notely.prompts.Prompt.ask", return_value="s"):
            assert pick_from_list(items, extras=extras) == "s"

    def test_free_text_when_allowed(self):
        items = [("a", "Alpha")]
        with patch("notely.prompts.Prompt.ask", return_value="clients/acme"):
            result = pick_from_list(items, allow_text=True)
        assert result == "clients/acme"

    def test_free_text_when_not_allowed_returns_none(self):
        items = [("a", "Alpha")]
        with patch("notely.prompts.Prompt.ask", return_value="clients/acme"):
            result = pick_from_list(items, allow_text=False)
        assert result is None

    def test_out_of_range_number_returns_none(self):
        items = [("a", "Alpha")]
        with patch("notely.prompts.Prompt.ask", return_value="5"):
            result = pick_from_list(items)
        assert result is None

    def test_keyboard_interrupt_returns_none(self):
        items = [("a", "Alpha")]
        with patch("notely.prompts.Prompt.ask", side_effect=KeyboardInterrupt):
            assert pick_from_list(items) is None

    def test_empty_items_with_extras(self):
        items = []
        extras = [("n", "New"), ("s", "Skip")]
        with patch("notely.prompts.Prompt.ask", return_value="n"):
            assert pick_from_list(items, extras=extras) == "n"


# ---------------------------------------------------------------------------
# duplicate_found
# ---------------------------------------------------------------------------

class TestDuplicateFound:
    """Tests for duplicate_found()."""

    def test_update(self):
        with patch("notely.prompts.Prompt.ask", return_value="u"):
            assert duplicate_found("My Note", "2026-01-01") == "update"

    def test_new(self):
        with patch("notely.prompts.Prompt.ask", return_value="n"):
            assert duplicate_found("My Note", "2026-01-01") == "new"

    def test_skip(self):
        with patch("notely.prompts.Prompt.ask", return_value="s"):
            assert duplicate_found("My Note", "2026-01-01") == "skip"

    def test_empty_defaults_to_skip(self):
        with patch("notely.prompts.Prompt.ask", return_value=""):
            assert duplicate_found("My Note", "2026-01-01") == "skip"

    def test_keyboard_interrupt_returns_skip(self):
        with patch("notely.prompts.Prompt.ask", side_effect=KeyboardInterrupt):
            assert duplicate_found("My Note", "2026-01-01") == "skip"

    def test_match_types_show_different_labels(self):
        """All three match types produce valid output."""
        for mt in ("exact", "near", "similar"):
            with patch("notely.prompts.Prompt.ask", return_value="u"):
                assert duplicate_found("Note", "2026-01-01", match_type=mt) == "update"


# ---------------------------------------------------------------------------
# confirm_destructive
# ---------------------------------------------------------------------------

class TestConfirmDestructive:
    """Tests for confirm_destructive()."""

    def test_yes(self):
        with patch("notely.prompts.Prompt.ask", return_value="y"):
            assert confirm_destructive("Delete?") is True

    def test_no(self):
        with patch("notely.prompts.Prompt.ask", return_value="n"):
            assert confirm_destructive("Delete?") is False

    def test_default_no(self):
        """Default is 'n' — pressing Enter means no."""
        with patch("notely.prompts.Prompt.ask", return_value="n") as mock:
            confirm_destructive("Delete?")
        assert mock.call_args[1]["default"] == "n"

    def test_keyboard_interrupt(self):
        with patch("notely.prompts.Prompt.ask", side_effect=KeyboardInterrupt):
            assert confirm_destructive("Delete?") is False


# ---------------------------------------------------------------------------
# no_changes_retry
# ---------------------------------------------------------------------------

class TestNoChangesRetry:
    """Tests for no_changes_retry()."""

    def test_describe(self):
        with patch("notely.prompts.Prompt.ask", return_value="d"):
            assert no_changes_retry() == "d"

    def test_skip(self):
        with patch("notely.prompts.Prompt.ask", return_value="s"):
            assert no_changes_retry() == "s"

    def test_default_is_skip(self):
        with patch("notely.prompts.Prompt.ask", return_value="s") as mock:
            no_changes_retry()
        assert mock.call_args[1]["default"] == "s"

    def test_keyboard_interrupt_returns_skip(self):
        with patch("notely.prompts.Prompt.ask", side_effect=KeyboardInterrupt):
            assert no_changes_retry() == "s"

    def test_invalid_input_returns_skip(self):
        with patch("notely.prompts.Prompt.ask", return_value="x"):
            assert no_changes_retry() == "s"
