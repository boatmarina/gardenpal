"""
Tests for fertilization scheduling logic.

Run with:  pytest tests/test_fertilization.py -v

These tests cover the pure date-computation functions in fertilization_logic.py.
No DB or API calls are made — the tests are fast and fully offline.

Key scenarios mirrored from real bugs:
- Bug A: AI returned Sep 21 for "28 days from Jul 15" (should be Aug 12)
- Bug B: When interval is missed, AI suggested next cycle instead of "do it now"
- Bug C: Scheduled date fell after fertilization_cutoff_date — contradiction
"""

import pytest
from gardenpal.fertilization_logic import (
    compute_next_fertilization_date,
    note_implies_not_needed,
    parse_ai_fertilization_response,
    repair_stored_fertilization_date,
)


# ---------------------------------------------------------------------------
# parse_ai_fertilization_response
# ---------------------------------------------------------------------------

class TestParseAiResponse:
    def test_full_valid_response(self):
        raw = "2025-09-15\nINTERVAL_DAYS: 28\nCUTOFF_DATE: 2025-10-31\nBlueberries benefit from monthly feeding."
        date_line, freq, cutoff, note = parse_ai_fertilization_response(raw)
        assert date_line == "2025-09-15"
        assert freq == 28
        assert cutoff == "2025-10-31"
        assert "Blueberries" in note

    def test_not_needed_response(self):
        raw = "NOT_NEEDED\nINTERVAL_DAYS: none\nCUTOFF_DATE: none\nPlant is dormant for the season."
        date_line, freq, cutoff, note = parse_ai_fertilization_response(raw)
        assert date_line.upper() == "NOT_NEEDED"
        assert freq is None
        assert cutoff is None
        assert "dormant" in note

    def test_missing_optional_fields(self):
        raw = "2025-08-20\nThis plant needs monthly feeding."
        date_line, freq, cutoff, note = parse_ai_fertilization_response(raw)
        assert date_line == "2025-08-20"
        assert freq is None
        assert cutoff is None
        assert "monthly" in note

    def test_invalid_interval_ignored(self):
        raw = "2025-08-20\nINTERVAL_DAYS: weekly\nNote about plant."
        _, freq, _, _ = parse_ai_fertilization_response(raw)
        assert freq is None

    def test_invalid_cutoff_date_ignored(self):
        raw = "2025-08-20\nINTERVAL_DAYS: 14\nCUTOFF_DATE: end-of-summer\nNote."
        _, _, cutoff, _ = parse_ai_fertilization_response(raw)
        assert cutoff is None

    def test_empty_response(self):
        date_line, freq, cutoff, note = parse_ai_fertilization_response("")
        assert date_line == ""
        assert freq is None
        assert cutoff is None
        assert note == ""


# ---------------------------------------------------------------------------
# note_implies_not_needed
# ---------------------------------------------------------------------------

class TestNoteImpliesNotNeeded:
    def test_skip_phrase(self):
        assert note_implies_not_needed("You should skip fertilizing this plant for now.")

    def test_avoid_phrase(self):
        assert note_implies_not_needed("Avoid fertilizing in late summer.")

    def test_do_not_phrase(self):
        assert note_implies_not_needed("Do not fertilize after August.")

    def test_should_not_phrase(self):
        assert note_implies_not_needed("You should not fertilize carrots to prevent forking.")

    def test_refrain_phrase(self):
        assert note_implies_not_needed("Refrain from fertilizing during dormancy.")

    def test_neutral_note_is_fine(self):
        assert not note_implies_not_needed("Apply a balanced fertilizer every 28 days.")

    def test_empty_note_is_fine(self):
        assert not note_implies_not_needed("")

    def test_case_insensitive(self):
        assert note_implies_not_needed("DO NOT FERTILIZE this plant.")


# ---------------------------------------------------------------------------
# compute_next_fertilization_date — core scheduling logic
# ---------------------------------------------------------------------------

class TestComputeNextFertilizationDate:

    # --- BUG A: AI arithmetic error ---

    def test_bug_a_ai_wrong_math_corrected(self):
        """
        Bug: AI said 'Sep 21' for 28 days from Jul 15. Correct answer is Aug 12.
        Scenario: interval not yet missed (today is Jul 20).
        """
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-09-21",
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-07-20",
        )
        assert not not_needed
        assert result == "2025-08-12"  # Jul 15 + 28 = Aug 12

    def test_ai_date_used_when_no_interval(self):
        """Without interval info, fall back to AI's date (possibly wrong, but nothing to correct from)."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-09-21",
            last_fert_str="2025-07-15",
            freq_days=None,
            cutoff_date=None,
            today_str="2025-07-20",
        )
        assert not not_needed
        assert result == "2025-09-21"

    def test_ai_date_used_when_no_last_fert(self):
        """Without a last-fert date, can't compute schedule — use AI's date."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-09-15",
            last_fert_str="never",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert not not_needed
        assert result == "2025-09-15"

    # --- BUG B: missed interval should suggest "do it now" ---
    # The code fix only corrects AI arithmetic for future schedule dates. For missed intervals
    # the AI prompt now instructs Claude to return today's date (not the next cycle date), so
    # these tests verify the code does NOT override a correct AI response for a missed interval.

    def test_bug_b_ai_says_today_for_missed_interval(self):
        """
        After the prompt fix, AI correctly returns today when interval was missed.
        Code should honor that and return today as-is.
        """
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-08-25",  # AI correctly said today (per prompt instruction)
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert not not_needed
        assert result == "2025-08-25"

    def test_bug_b_ai_future_date_honored_when_interval_passed(self):
        """
        Regression test for the 'Due today' bug (screenshot Aug 2026).
        AI recommended Sep 8 for a tomato — a deliberate future recommendation, not a
        rolled-forward cycle. When sched (last_fert + interval) is already past, we trust
        the AI's future date rather than overriding to today.
        """
        result, not_needed = compute_next_fertilization_date(
            ai_date="2026-09-08",   # AI's deliberate recommendation
            last_fert_str="2026-07-27",
            freq_days=14,           # biweekly interval — sched = Aug 10, already past
            cutoff_date=None,
            today_str="2026-08-25",
        )
        assert not not_needed
        assert result == "2026-09-08"  # AI's date kept, NOT overridden to "today"

    def test_interval_not_missed_waits_correctly(self):
        """Aug 1 + 28 = Aug 29. Today is Aug 25, so Aug 29 is still in the future — wait."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-08-29",
            last_fert_str="2025-08-01",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert not not_needed
        assert result == "2025-08-29"

    def test_just_fertilized_today_schedules_future(self):
        """Fertilized today — next should be today + interval, not today."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-09-22",
            last_fert_str="2025-08-25",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert not not_needed
        assert result == "2025-09-22"  # Aug 25 + 28 = Sep 22

    # --- BUG C: date past cutoff ---

    def test_bug_c_date_past_cutoff_before_cutoff_suggests_today(self):
        """
        Bug: computed date (Aug 12 rolled to today Aug 25) still before cutoff (Aug 31).
        Sep 21 > Aug 31, and today <= cutoff → fertilize today before season ends.
        """
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-09-21",
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date="2025-08-31",
            today_str="2025-08-25",
        )
        assert not not_needed
        assert result == "2025-08-25"  # ASAP before cutoff

    def test_bug_c_date_past_cutoff_after_cutoff_is_not_needed(self):
        """Past the cutoff entirely — skip until next season."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-09-21",
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date="2025-08-31",
            today_str="2025-09-05",
        )
        assert not_needed
        assert result is None

    def test_cutoff_respected_even_without_interval(self):
        """Cutoff check applies regardless of whether we have interval info."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-10-01",
            last_fert_str="never",
            freq_days=None,
            cutoff_date="2025-09-30",
            today_str="2025-09-15",
        )
        assert not not_needed
        assert result == "2025-09-15"  # fertilize before cutoff

    def test_date_before_cutoff_is_unchanged(self):
        """If the computed date is before the cutoff, no clamping."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-08-12",
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date="2025-10-31",
            today_str="2025-07-20",
        )
        assert not not_needed
        assert result == "2025-08-12"

    def test_no_cutoff_no_clamping(self):
        """No cutoff — dates well into the future are fine."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-12-01",
            last_fert_str="2025-11-03",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-11-10",
        )
        assert not not_needed
        assert result == "2025-12-01"  # Nov 3 + 28 = Dec 1

    # --- AI date in the past (no interval info) ---

    def test_ai_date_in_past_clamped_to_today(self):
        """AI returned a past date and we have no interval to correct from."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-07-01",
            last_fert_str="never",
            freq_days=None,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert not not_needed
        assert result == "2025-08-25"

    # --- Edge cases ---

    def test_last_fert_empty_string_treated_as_never(self):
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-09-01",
            last_fert_str="",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert not not_needed
        assert result == "2025-09-01"  # no schedule override, AI date used

    def test_cutoff_same_as_today_still_ok(self):
        """Cutoff is today — still schedule for today (not past)."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-10-01",
            last_fert_str="never",
            freq_days=None,
            cutoff_date="2025-08-25",
            today_str="2025-08-25",
        )
        assert not not_needed
        assert result == "2025-08-25"

    def test_freq_zero_treated_as_falsy(self):
        """freq_days=0 is falsy — fall back to AI date."""
        result, not_needed = compute_next_fertilization_date(
            ai_date="2025-09-01",
            last_fert_str="2025-08-01",
            freq_days=0,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert not not_needed
        assert result == "2025-09-01"


# ---------------------------------------------------------------------------
# repair_stored_fertilization_date
# ---------------------------------------------------------------------------

class TestRepairStoredFertilizationDate:

    def test_future_wrong_date_corrected(self):
        """
        sched = Jul 15 + 28 = Aug 12, still in the future (today = Jul 20).
        Stored Sep 21 is an AI arithmetic error — repair to Aug 12.
        """
        result = repair_stored_fertilization_date(
            stored_date="2025-09-21",
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-07-20",
        )
        assert result == "2025-08-12"

    def test_missed_interval_stored_future_repaired_to_today(self):
        """
        sched = Jul 15 + 28 = Aug 12 — already past (today = Aug 25).
        Stored date Sep 21 is in the future → AI arithmetic error (probably 'next cycle').
        Repair to today so user sees it as overdue.
        """
        result = repair_stored_fertilization_date(
            stored_date="2025-09-21",
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert result == "2025-08-25"

    def test_missed_interval_stored_today_unchanged(self):
        """
        sched = Aug 12 (past). Stored date is today (Aug 25) — already correct, no repair.
        """
        result = repair_stored_fertilization_date(
            stored_date="2025-08-25",
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert result == "2025-08-25"  # unchanged

    def test_correct_future_date_unchanged(self):
        """Stored date already matches schedule — no repair needed."""
        result = repair_stored_fertilization_date(
            stored_date="2025-08-29",
            last_fert_str="2025-08-01",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert result == "2025-08-29"

    def test_past_cutoff_returns_none(self):
        """sched = Aug 12 (past). Stored Sep 21 is in the future but today > cutoff → not needed."""
        result = repair_stored_fertilization_date(
            stored_date="2025-09-21",
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date="2025-08-31",
            today_str="2025-09-05",
        )
        assert result is None

    def test_no_last_fert_returns_stored_unchanged(self):
        result = repair_stored_fertilization_date(
            stored_date="2025-09-21",
            last_fert_str="never",
            freq_days=28,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert result == "2025-09-21"

    def test_no_freq_returns_stored_unchanged(self):
        result = repair_stored_fertilization_date(
            stored_date="2025-09-21",
            last_fert_str="2025-07-15",
            freq_days=None,
            cutoff_date=None,
            today_str="2025-08-25",
        )
        assert result == "2025-09-21"

    def test_before_cutoff_clamped_to_today(self):
        """
        sched = Aug 12 (past). Stored Sep 21 is future. today(Aug 25) still before cutoff(Aug 31)
        → repair to today so the user gets the overdue alert before the season ends.
        """
        result = repair_stored_fertilization_date(
            stored_date="2025-09-21",
            last_fert_str="2025-07-15",
            freq_days=28,
            cutoff_date="2025-08-31",
            today_str="2025-08-25",
        )
        assert result == "2025-08-25"
