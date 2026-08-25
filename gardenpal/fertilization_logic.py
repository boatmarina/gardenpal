"""
Pure functions for fertilization scheduling logic.
No external dependencies (no Flask, no DB, no Anthropic) so they can be unit-tested easily.
"""

import re as _re
from datetime import date, timedelta

_ISO_DATE_RE = _re.compile(r'^\d{4}-\d{2}-\d{2}$')

_NOT_NEEDED_PHRASES = [
    "skip fertiliz",
    "avoid fertiliz",
    "do not fertiliz",
    "don't fertiliz",
    "should not fertiliz",
    "not recommended to fertiliz",
    "harmful to fertiliz",
    "forgo fertiliz",
    "refrain from fertiliz",
]


def note_implies_not_needed(text: str) -> bool:
    """Return True if the note text advises against fertilizing."""
    tl = text.lower()
    return any(p in tl for p in _NOT_NEEDED_PHRASES)


def parse_ai_fertilization_response(raw: str):
    """
    Parse the structured AI fertilization response.

    Expected format:
      Line 1: YYYY-MM-DD  OR  NOT_NEEDED
      Line 2: INTERVAL_DAYS: <int>  OR  INTERVAL_DAYS: none
      Line 3: CUTOFF_DATE: <YYYY-MM-DD>  OR  CUTOFF_DATE: none
      Line 4+: free-text explanation

    Returns (date_line, freq_days, cutoff_date, note_text).
    """
    lines = raw.strip().splitlines()
    date_line = lines[0].strip() if lines else ""
    freq_days = None
    cutoff_date = None
    note_lines = []
    for ln in lines[1:]:
        ul = ln.strip().upper()
        if ul.startswith("INTERVAL_DAYS:"):
            val = ln.split(":", 1)[1].strip()
            if val.isdigit():
                freq_days = int(val)
        elif ul.startswith("CUTOFF_DATE:"):
            val = ln.split(":", 1)[1].strip()
            if _ISO_DATE_RE.match(val):
                cutoff_date = val
        else:
            if ln.strip():
                note_lines.append(ln)
    note_text = "\n".join(note_lines).strip()
    return date_line, freq_days, cutoff_date, note_text


def compute_next_fertilization_date(
    ai_date: str,
    last_fert_str: str,
    freq_days,
    cutoff_date,
    today_str: str,
):
    """
    Pure function: compute the correct next fertilization date given AI output and schedule context.

    Args:
        ai_date:      The date on line 1 of the AI response (YYYY-MM-DD, already validated).
        last_fert_str: Last fertilization date string (YYYY-MM-DD), or "never"/empty/"".
        freq_days:    Integer interval between fertilizations, or None.
        cutoff_date:  Last date fertilizing is beneficial this season (YYYY-MM-DD), or None.
        today_str:    Today's date (YYYY-MM-DD).

    Returns:
        (date_str_or_None, is_not_needed: bool)

    Logic:
    1. Start with ai_date, clamped to today if the AI gave a past date.
    2. If freq_days and last_fert_str are both available AND the schedule date is still in the
       future (>= today), use the computed date instead of the AI's guess. This corrects AI
       arithmetic errors (e.g. "28 days from Jul 15 = Sep 21") for not-yet-due intervals.
       When the interval has already passed, the AI's date is kept — the prompt instructs the
       AI to return today's date for missed intervals, so we trust its judgment about whether
       to apply now or wait for a contextual reason (e.g. "wait until after expected rain").
    3. If the result exceeds cutoff_date: pull to today if there's still time before cutoff;
       otherwise return (None, True) meaning "skip until next season".
    """
    d = ai_date if ai_date >= today_str else today_str

    if freq_days and last_fert_str and last_fert_str not in ("never", "") and _ISO_DATE_RE.match(last_fert_str):
        try:
            sched = (date.fromisoformat(last_fert_str) + timedelta(days=freq_days)).isoformat()
            if sched >= today_str:
                # Interval not yet due — override AI's arithmetic with computed date
                d = sched
            # else: interval already passed — keep AI's date unchanged (AI prompt handles this)
        except (ValueError, TypeError):
            pass

    if cutoff_date and d > cutoff_date:
        if today_str <= cutoff_date:
            return today_str, False
        return None, True

    return d, False


def repair_stored_fertilization_date(
    stored_date: str,
    last_fert_str: str,
    freq_days,
    cutoff_date,
    today_str: str,
):
    """
    Lightweight repair for a date stored in the DB that was computed under old (buggy) logic.
    Returns the corrected date string, None if the plant should be marked not-needed, or
    stored_date unchanged if there is not enough info or nothing to correct.

    Used on page load to silently fix wrong dates without re-calling Claude.

    Repair strategy:
    - If the computed schedule date (last_fert + freq_days) is still in the future and differs
      from stored_date, the AI made an arithmetic error — correct it to the computed date.
    - If the computed schedule date is already past and stored_date is a future date, the AI
      made an arithmetic error placing it too far ahead — repair to today (do it now).
    - If the computed schedule date is already past and stored_date is today or past, no repair
      is needed (AI correctly identified it as due now/overdue).
    - Cutoff enforcement applies in all cases.
    """
    if not (freq_days and last_fert_str and last_fert_str not in ("never", "") and _ISO_DATE_RE.match(last_fert_str)):
        return stored_date

    try:
        sched = (date.fromisoformat(last_fert_str) + timedelta(days=freq_days)).isoformat()
        if sched >= today_str:
            correct = sched
        elif stored_date > today_str:
            # Interval passed but stored date is still in the future — AI arithmetic error
            correct = today_str
        else:
            # Interval passed, stored date is today or overdue — already correct
            return stored_date
    except (ValueError, TypeError):
        return stored_date

    if cutoff_date and correct > cutoff_date:
        if today_str <= cutoff_date:
            correct = today_str
        else:
            return None

    return correct


# ---------------------------------------------------------------------------
# Planned-date helpers
# ---------------------------------------------------------------------------

def is_planned_stale(planned_date_str, last_fert_str):
    """
    Return True if a manually-set planned date has been superseded by an actual
    fertilization that happened on or after it.

    A planned date of "2025-08-10" is stale once last_fert_str >= "2025-08-10",
    meaning the user already fertilized on or after the planned day.

    Returns False when either argument is missing/invalid.
    """
    if not (planned_date_str and last_fert_str
            and planned_date_str not in ("never", "")
            and last_fert_str not in ("never", "")
            and _ISO_DATE_RE.match(planned_date_str)
            and _ISO_DATE_RE.match(last_fert_str)):
        return False
    return last_fert_str >= planned_date_str


def resolve_effective_fert_date(planned_date_str, next_fert_str, last_fert_str):
    """
    Return the date that should actually be shown to the user.

    A manually planned date takes priority over the AI-computed next_fert date,
    UNLESS that planned date is stale (the user already fertilized on/after it).

    Args:
        planned_date_str: Manually set planned date (YYYY-MM-DD), or None/"".
        next_fert_str:    AI/schedule-computed next date (YYYY-MM-DD), or None/"".
        last_fert_str:    Last fertilization date (YYYY-MM-DD), or "never"/"".

    Returns the winning date string, or None if nothing is available.
    """
    has_planned = (planned_date_str
                   and planned_date_str not in ("never", "")
                   and _ISO_DATE_RE.match(planned_date_str))
    if has_planned and not is_planned_stale(planned_date_str, last_fert_str):
        return planned_date_str

    has_next = (next_fert_str
                and next_fert_str not in ("never", "")
                and _ISO_DATE_RE.match(next_fert_str))
    return next_fert_str if has_next else None


def should_clear_planned_date(planned_date_str, new_last_fert_str):
    """
    Return True if recording a new fertilization event should also clear the
    manually planned date.

    Clears when:
    - A valid planned date exists, AND
    - new_last_fert_str >= planned_date_str (the new fertilization covers the plan)

    Returns False when either value is missing/invalid (no clearing needed).
    """
    return is_planned_stale(planned_date_str, new_last_fert_str)
