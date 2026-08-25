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
    2. If freq_days and last_fert_str are both available, override with the schedule-derived date
       (last_fert + freq_days). This corrects AI arithmetic errors and ensures missed intervals
       suggest "today" rather than rolling forward to the next cycle.
    3. If the result exceeds cutoff_date: pull to today if there's still time before cutoff;
       otherwise return (None, True) meaning "skip until next season".
    """
    d = ai_date if ai_date >= today_str else today_str

    if freq_days and last_fert_str and last_fert_str not in ("never", "") and _ISO_DATE_RE.match(last_fert_str):
        try:
            sched = (date.fromisoformat(last_fert_str) + timedelta(days=freq_days)).isoformat()
            d = today_str if sched < today_str else sched
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
    Returns the corrected date string, or None if the plant should be marked not-needed.
    Returns stored_date unchanged if there is not enough info to recompute.

    Used on page load to silently fix wrong dates without re-calling Claude.
    """
    if not (freq_days and last_fert_str and last_fert_str not in ("never", "") and _ISO_DATE_RE.match(last_fert_str)):
        return stored_date

    try:
        sched = (date.fromisoformat(last_fert_str) + timedelta(days=freq_days)).isoformat()
        correct = today_str if sched < today_str else sched
    except (ValueError, TypeError):
        return stored_date

    if cutoff_date and correct > cutoff_date:
        if today_str <= cutoff_date:
            correct = today_str
        else:
            return None

    return correct
