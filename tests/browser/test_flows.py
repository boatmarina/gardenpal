import io
import re

import pytest
from PIL import Image

from tests.test_ai_actions import fake_ai, reply, tool_call  # noqa: F401 (fixture)
from tests.test_garden import AJAX


def seed_entry(page, db, name="Leeks", notes=("Planted out", "Thinned to 6in", "Still harvesting")):
    page.api.post("/garden/new", data={"plant_name": name, "variety": "Autumn Giant",
                                       "planted_date": "2026-06-01", "zone_name": "Middle North"})
    entry = db.one("SELECT * FROM garden_entries WHERE plant_name = ?", (name,))
    for i, text in enumerate(notes):
        page.api.post(f"/garden/{entry['id']}/photos",
                      data={"photo_notes": text, "photo_date": f"2026-0{7 + i}-15"}, headers=AJAX)
    return entry


def growth_entries(page):
    return page.locator("#growth-log-entries > div")


def open_note_form(page):
    """The add-note form starts collapsed behind a button."""
    assert page.locator("#garden-add-note-panel").is_hidden(), "add-note form should start collapsed"
    page.click("#garden-add-note-toggle")
    page.wait_for_selector("#garden-note-input", state="visible")


def note_texts(page):
    """Each growth-log entry's note text (its last non-empty line; WebKit adds a trailing newline)."""
    return [[ln for ln in t.splitlines() if ln.strip()][-1] for t in growth_entries(page).all_inner_texts()]


def test_main_pages_load_without_js_errors(logged_in, db):
    page = logged_in
    entry = seed_entry(page, db)
    page.api.post("/ideas/new", data={"name": "Coneflower", "lookup_status": "draft"})
    plant = db.one("SELECT id FROM plants")
    paths = ["/", "/garden", f"/garden/{entry['id']}", "/garden/new", "/ideas", "/ideas/new",
             f"/ideas/{plant['id']}", "/yard", f"/yard/zones/{entry['zone_id']}", "/tools"]
    for path in paths:
        page.goto(path)
        page.wait_for_load_state("networkidle")
        assert page.js_errors == [], f"{path}: {page.js_errors}"
        assert page.dialogs == [], f"{path}: unexpected alert {page.dialogs}"


def test_add_note_shows_it_without_reload(logged_in, db):
    page = logged_in
    entry = seed_entry(page, db)
    page.goto(f"/garden/{entry['id']}")
    before = growth_entries(page).count()

    open_note_form(page)
    assert page.evaluate("document.activeElement.id") == "garden-note-input", "opening should focus the note field"
    page.fill("#garden-note-input", "Beans & peas <3 with <b>big</b> pods")
    page.wait_for_timeout(500)  # let the delayed focus-scroll run, as with real typing
    with page.expect_response(re.compile(r"/photos$")):
        page.click("#garden-photo-submit-btn")
    page.wait_for_function(f"document.querySelectorAll('#growth-log-entries > div').length > {before}")
    page.wait_for_timeout(600)

    assert page.dialogs == [], "saving showed an alert (e.g. 'Save failed')"
    first = growth_entries(page).first
    assert "Beans & peas <3 with <b>big</b> pods" in first.inner_text()
    assert first.bounding_box()["y"] < page.viewport_size["height"], "new note should be scrolled into view"
    assert page.input_value("#garden-note-input") == ""
    assert page.locator("#garden-add-note-panel").is_hidden(), "form should collapse after saving"
    assert page.locator("#garden-add-note-toggle").is_visible()
    assert page.locator("#garden-photo-loading").is_hidden()
    assert page.locator("#garden-photo-submit-btn").is_enabled()
    assert page.evaluate("window.scrollY") == 0, "the whole page shifted; header would be off-screen"
    assert page.locator(".back-btn").is_visible()

    page.reload()
    assert "Beans & peas <3 with <b>big</b> pods" in growth_entries(page).first.inner_text()


def test_add_photo_note(logged_in, db, tmp_path):
    page = logged_in
    entry = seed_entry(page, db, notes=())
    img = tmp_path / "leaf.png"
    Image.new("RGB", (300, 200), (80, 140, 60)).save(img)
    page.goto(f"/garden/{entry['id']}")

    open_note_form(page)
    page.set_input_files("#garden-photo-input", str(img))
    page.wait_for_selector("#garden-photo-preview:not([hidden])")
    page.click("#garden-photo-submit-btn")
    new_img = page.locator("#growth-log-entries > div img").first
    new_img.wait_for()
    page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=new_img.element_handle())
    assert page.dialogs == []
    assert page.locator("#garden-photo-preview").is_hidden()


def test_back_button_sticks_flush_under_header(logged_in, db):
    """WebKit offsets sticky elements by the scroll container's padding; a gap let content show through."""
    page = logged_in
    entry = seed_entry(page, db, notes=[f"Note {i}" for i in range(6)])
    page.goto(f"/garden/{entry['id']}")
    page.evaluate("document.querySelector('main').scrollTop = 99999")
    page.wait_for_timeout(200)

    main_top = page.evaluate("document.querySelector('main').getBoundingClientRect().top")
    btn = page.locator(".back-btn").bounding_box()
    assert abs(btn["y"] - main_top) <= 1, f"back button sticks {btn['y'] - main_top:.1f}px below the header"
    hit = page.evaluate(f"document.elementFromPoint(20, {main_top + 2}).closest('.back-btn') !== null")
    assert hit, "page content is visible between the header and the back button"


def test_toggle_done_for_season(logged_in, db):
    page = logged_in
    entry = seed_entry(page, db, notes=())
    page.goto(f"/garden/{entry['id']}")
    with page.expect_response(re.compile(r"/toggle-done$")):
        page.click("#done-season-btn")
    page.wait_for_selector("#done-season-chip", state="visible")
    assert page.locator("#fert-section").is_hidden()
    assert db.one("SELECT done_for_season FROM garden_entries")["done_for_season"] == 1


def test_entry_chat_reports_unconfigured_assistant(logged_in, db):
    page = logged_in
    entry = seed_entry(page, db, notes=())
    page.goto(f"/garden/{entry['id']}")
    page.click("#entry-chat-toggle")
    page.fill("#entry-chat-input", "When should I harvest?")
    page.click("#entry-chat-send")
    page.wait_for_selector("#entry-chat-messages >> text=not configured")
    assert page.locator("#entry-chat-input").is_enabled()


def test_add_edible_through_form(logged_in, db):
    page = logged_in
    page.goto("/garden/new")
    page.fill("input[name=plant_name]", "Sungold tomato")
    with page.expect_navigation():
        page.click("button:has-text('Save to Edibles')")
    assert re.search(r"/garden/\d+$", page.url)
    assert "Sungold tomato" in page.inner_text("h2")


@pytest.mark.parametrize("mode", ["name", "url"])
def test_add_ornamental(logged_in, db, mode):
    page = logged_in
    page.goto("/ideas/new")
    page.click(f".mode-card[data-mode={mode}]")
    field = "#plant-name-input" if mode == "name" else "#url-name-input"
    page.fill(field, "Japanese Anemone")
    page.keyboard.press("Escape")
    with page.expect_navigation(timeout=15000):
        page.click("#save-idea-btn")
    assert re.search(r"/ideas/\d+$", page.url), page.url
    assert db.one("SELECT name FROM plants")["name"] == "Japanese Anemone"


def test_growth_log_form_is_above_notes_and_new_notes_land_in_date_order(logged_in, db):
    page = logged_in
    entry = seed_entry(page, db)  # notes dated Jul 15, Aug 15, Sep 15
    page.goto(f"/garden/{entry['id']}")
    form_y = page.locator("#garden-add-note").bounding_box()["y"]
    assert form_y < growth_entries(page).first.bounding_box()["y"], "add-note form should sit above the notes"

    open_note_form(page)
    page.fill("#garden-note-input", "Backfilled: first true leaves")
    page.fill("input[name=photo_date]", "2026-08-01")
    page.click("#garden-photo-submit-btn")
    page.wait_for_function("document.querySelectorAll('#growth-log-entries > div').length === 4")
    texts = note_texts(page)
    assert texts == ["Still harvesting", "Thinned to 6in", "Backfilled: first true leaves", "Planted out"]

    page.reload()
    assert note_texts(page) == texts


def test_plant_chat_can_mark_done_for_season(logged_in, db, fake_ai):
    page = logged_in
    entry = seed_entry(page, db, notes=())
    fake_ai.script += [reply("", tool_call("set_done_for_season", done=True), stop="tool_use"),
                       reply("Marked your leeks as done for the season.")]
    page.goto(f"/garden/{entry['id']}")
    page.click("#entry-chat-toggle")
    page.fill("#entry-chat-input", "These are finished, I pulled them")
    page.click("#entry-chat-send")
    page.wait_for_selector("#done-season-chip", state="visible")
    assert page.inner_text("#done-season-label") == "Done for the season"
    assert page.locator("#fert-section").is_hidden()
    assert db.one("SELECT done_for_season FROM garden_entries")["done_for_season"] == 1


def test_add_note_cancel_collapses_form(logged_in, db):
    page = logged_in
    entry = seed_entry(page, db, notes=("Planted out",))
    page.goto(f"/garden/{entry['id']}")
    toggle = page.locator("#garden-add-note-toggle")
    assert toggle.is_visible() and "Add a note" in toggle.inner_text()
    open_note_form(page)
    assert toggle.is_hidden()
    page.click("#garden-add-note-cancel")
    assert page.locator("#garden-add-note-panel").is_hidden()
    assert toggle.is_visible()
    assert db.one("SELECT COUNT(*) AS n FROM garden_photos")["n"] == 1
