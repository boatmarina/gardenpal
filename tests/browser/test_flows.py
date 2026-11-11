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


def test_zone_page_filters_plants_by_bed(logged_in, db):
    page = logged_in
    for name, bed in (("Peas", "Peas bed"), ("Snap peas", "Peas bed"), ("Kale", "Brassica bed"), ("Garlic", None)):
        data = {"plant_name": name, "zone_name": "Middle North"}
        if bed:
            data["location_name"] = bed
        page.api.post("/garden/new", data=data)
    zone = db.one("SELECT id FROM yard_zones")
    page.api.post("/yard/plants/new", data={"zone_id": zone["id"], "plant_name": "Lavender"})
    page.goto(f"/yard/zones/{zone['id']}")

    def visible_cards():
        return sorted(t.splitlines()[0].strip() for t in page.locator("article.card:visible h3").all_inner_texts())

    assert visible_cards() == ["Garlic", "Kale", "Lavender", "Peas", "Snap peas"]
    page.click(".bed-chip[data-bed='peas bed']")
    assert visible_cards() == ["Peas", "Snap peas"]
    assert page.locator("#zone-ornamentals").is_hidden(), "ornamentals aren't in beds"
    assert "bed=peas+bed" in page.url or "bed=peas%20bed" in page.url

    page.click(".bed-chip[data-bed='__none']")
    assert visible_cards() == ["Garlic", "Lavender"]

    page.reload()  # the filter survives a reload via the URL
    assert visible_cards() == ["Garlic", "Lavender"]
    page.click(".bed-chip[data-bed='__all']")
    assert len(visible_cards()) == 5


def test_add_bed_then_record_details_and_notes(logged_in, db):
    page = logged_in
    page.api.post("/yard/zones/new", data={"name": "Back deck"})
    zone = db.one("SELECT id FROM yard_zones")
    page.goto(f"/yard/zones/{zone['id']}")
    assert page.locator("#bed-add-form").is_hidden()
    page.click("#bed-add-toggle")
    page.fill("#bed-add-name", "Herb planter")
    with page.expect_navigation():
        page.click("#bed-add-form button[type=submit]")
    assert re.search(r"/yard/beds/\d+$", page.url)

    page.click("#bed-edit-toggle")
    page.fill("input[name=sun_exposure]", "Full sun until 3pm")
    page.fill("textarea[name=soil_notes]", "Potting mix + worm castings")
    with page.expect_navigation():
        page.click("#bed-edit-form button[type=submit]")
    assert "Full sun until 3pm" in page.inner_text("#bed-details")
    assert "worm castings" in page.inner_text("#bed-details")

    assert page.locator("#bed-note-form").is_hidden()
    page.click("#bed-note-toggle")
    page.fill("#bed-note-input", "Top-dressed with compost")
    with page.expect_navigation():
        page.click("#bed-note-form button[type=submit]")
    assert "Top-dressed with compost" in page.inner_text("#bed-notes")

    page.click(".back-btn")
    page.wait_for_url(re.compile(rf"/yard/zones/{zone['id']}$"))
    row = page.locator(".bed-row", has_text="Herb planter")
    assert "Full sun until 3pm" in row.inner_text() and "1 note" in row.inner_text()


def test_edible_form_bed_picker(logged_in, db):
    page = logged_in
    page.api.post("/garden/new", data={"plant_name": "Peas", "zone_name": "Middle North", "location_name": "Pea trellis"})
    zone = db.one("SELECT id FROM yard_zones")
    page.api.post(f"/yard/zones/{zone['id']}/beds", data={"name": "Raised bed 1"})
    bed = db.one("SELECT id FROM yard_beds WHERE name = 'Raised bed 1'")
    page.api.post(f"/yard/beds/{bed['id']}/edit", data={"name": "Raised bed 1", "bed_type": "raised_bed"})

    page.goto("/garden/new")
    assert page.locator("#bed-select").is_hidden(), "no zone yet: free-text location"
    assert page.locator("#loc-name-input").is_visible()
    page.fill("input[name=plant_name]", "Leeks")
    page.click("#zone-name-input")
    page.locator("#zone-name-dropdown li", has_text="Middle North").click()
    assert page.locator("#bed-select").is_visible()
    options = page.locator("#bed-select option").all_inner_texts()
    assert options == ["No bed", "Pea trellis", "Raised bed 1", "+ New bed…"]
    assert page.locator("#loc-name-input").is_hidden()

    type_field = page.locator("#location-type-field")
    assert type_field.is_visible(), "no bed chosen yet: the type can still be set"
    page.select_option("#bed-select", "Raised bed 1")
    assert type_field.is_hidden(), "an existing bed already has its type"
    assert page.input_value("select[name=location_type]") == "raised_bed"
    page.select_option("#bed-select", "__new")
    assert type_field.is_visible(), "a new bed needs a type"
    assert page.locator("#loc-name-dropdown").is_hidden(), "naming a new bed shouldn't suggest existing ones"
    page.select_option("#bed-select", "Pea trellis")
    assert type_field.is_hidden()
    page.select_option("#bed-select", "")
    assert type_field.is_visible()
    page.select_option("#bed-select", "Raised bed 1")
    with page.expect_navigation():
        page.click("button:has-text('Save to Edibles')")
    assert db.one("SELECT location_name FROM garden_entries WHERE plant_name = 'Leeks'")["location_name"] == "Raised bed 1"

    page.goto("/garden/new")
    page.fill("input[name=plant_name]", "Chard")
    page.click("#zone-name-input")
    page.locator("#zone-name-dropdown li", has_text="Middle North").click()
    page.select_option("#bed-select", "__new")
    assert page.locator("#loc-name-input").is_visible()
    page.fill("#loc-name-input", "Chard bed")
    page.select_option("select[name=location_type]", "grow_bag")
    with page.expect_navigation():
        page.click("button:has-text('Save to Edibles')")
    assert db.one("SELECT location_name FROM garden_entries WHERE plant_name = 'Chard'")["location_name"] == "Chard bed"
    page.goto(f"/yard/zones/{zone['id']}")
    assert db.one("SELECT bed_type FROM yard_beds WHERE name = 'Chard bed'")["bed_type"] == "grow_bag"

    entry = db.one("SELECT id FROM garden_entries WHERE plant_name = 'Leeks'")
    page.goto(f"/garden/{entry['id']}/edit")
    assert page.input_value("#bed-select") == "Raised bed 1", "edit form preselects the plant's bed"
    assert page.locator("#location-type-field").is_hidden(), "editing a plant in an existing bed hides the type"

    # Switching to a zone with no beds brings the type field back with the free-text location.
    page.api.post("/yard/zones/new", data={"name": "Empty zone"})
    page.goto(f"/garden/{entry['id']}/edit")
    page.fill("#zone-name-input", "Empty")
    page.locator("#zone-name-dropdown li", has_text="Empty zone").click()
    assert page.locator("#bed-select").is_hidden()
    assert page.locator("#location-type-field").is_visible()


def test_ornamental_zone_card_edits_zone_and_bed_together(logged_in, db):
    page = logged_in
    page.api.post("/yard/zones/new", data={"name": "Front yard"})
    page.api.post("/yard/zones/new", data={"name": "Back yard"})
    front = db.one("SELECT id FROM yard_zones WHERE name = 'Front yard'")
    back = db.one("SELECT id FROM yard_zones WHERE name = 'Back yard'")
    page.api.post(f"/yard/zones/{front['id']}/beds", data={"name": "Front pots"})
    page.api.post(f"/yard/zones/{back['id']}/beds", data={"name": "Under plum tree"})
    page.api.post("/yard/plants/new", data={"zone_id": front["id"], "plant_name": "Lavender"})
    yp = db.one("SELECT id FROM yard_plants")
    page.api.post(f"/yard/plants/{yp['id']}/notes", data={"notes": "Bloomed early"})
    lib = db.one("SELECT id FROM plants WHERE name = 'Lavender'")

    page.goto(f"/ideas/{lib['id']}")
    card = page.locator(".pz-card")
    assert "Front yard" in card.locator(".pz-placement").inner_text()
    assert card.locator(".pz-bed-value").inner_text() == "No bed"
    assert card.locator(".pz-bed-form").is_hidden(), "zone and bed show as text until you tap the pencil"

    # Set a bed in the same zone.
    card.locator(".pz-edit-toggle").click()
    assert card.locator(".pz-placement").is_hidden()
    assert card.locator(".pz-bed-select option").all_inner_texts() == ["No bed", "Front pots", "+ New bed…"]
    card.locator(".pz-bed-select").select_option(label="Front pots")
    with page.expect_navigation():
        card.locator(".pz-bed-form button[type=submit]").click()
    assert page.locator(".pz-card .pz-bed-value").inner_text() == "Front pots"

    # Cancel puts everything back.
    page.locator(".pz-edit-toggle").click()
    page.locator(".pz-zone-select").select_option(label="Back yard")
    page.locator(".pz-edit-cancel").click()
    assert page.locator(".pz-bed-form").is_hidden()
    page.locator(".pz-edit-toggle").click()
    assert page.input_value(".pz-zone-select") == str(front["id"])
    assert page.locator(".pz-bed-select option:checked").inner_text() == "Front pots"

    # Move to another zone: the bed list follows the zone, and notes come along.
    page.locator(".pz-zone-select").select_option(label="Back yard")
    assert page.locator(".pz-bed-select option").all_inner_texts() == ["No bed", "Under plum tree", "+ New bed…"]
    page.locator(".pz-bed-select").select_option(label="Under plum tree")
    with page.expect_navigation():
        page.locator(".pz-bed-form button[type=submit]").click()
    row = db.one("SELECT yp.zone_id, b.name AS bed FROM yard_plants yp LEFT JOIN yard_beds b ON b.id = yp.bed_id")
    assert row == {"zone_id": back["id"], "bed": "Under plum tree"}
    assert "Back yard" in page.locator(".pz-placement").inner_text()
    assert "Bloomed early" in page.locator(".pz-card").inner_text()

    # A new bed from the editor.
    page.locator(".pz-edit-toggle").click()
    page.locator(".pz-bed-select").select_option("__new")
    page.fill(".pz-bed-new input", "Window box")
    with page.expect_navigation():
        page.locator(".pz-bed-form button[type=submit]").click()
    assert page.locator(".pz-card .pz-bed-value").inner_text() == "Window box"


def test_add_ornamental_from_yard_offers_zone_beds(logged_in, db):
    page = logged_in
    page.api.post("/yard/zones/new", data={"name": "Front yard"})
    page.api.post("/yard/zones/new", data={"name": "Back yard"})
    front = db.one("SELECT id FROM yard_zones WHERE name = 'Front yard'")
    back = db.one("SELECT id FROM yard_zones WHERE name = 'Back yard'")
    page.api.post(f"/yard/zones/{front['id']}/beds", data={"name": "Front pots"})

    page.goto(f"/yard/plants/new?zone_id={front['id']}")
    page.wait_for_load_state("networkidle")
    zone_sel = page.locator('#common-fields select[name="zone_id"]')
    if page.locator("#common-fields").is_hidden():
        page.evaluate("document.getElementById('common-fields').hidden = false")
    assert page.locator("#yard-bed-field").is_visible()
    assert page.locator("#yard-bed-select option").all_inner_texts() == ["No bed", "Front pots", "+ New bed…"]
    zone_sel.select_option(str(back["id"]))
    assert page.locator("#yard-bed-select option").all_inner_texts() == ["No bed", "+ New bed…"]
    page.select_option("#yard-bed-select", "__new")
    assert page.locator("#yard-bed-new input").is_visible()


def test_merge_bed_suggests_target_and_spot(logged_in, db):
    page = logged_in
    for name, loc in (("Kale", "Raised bed 1"), ("Peas", "Raised bed 1 - north end"), ("Basil", "Herb pot")):
        page.api.post("/garden/new", data={"plant_name": name, "zone_name": "Garden", "location_name": loc})
    zone = db.one("SELECT id FROM yard_zones")
    page.goto(f"/yard/zones/{zone['id']}")
    page.locator(".bed-row", has_text="north end").click()
    page.wait_for_url(re.compile(r"/yard/beds/\d+$"))

    assert page.locator("#bed-merge-form").is_hidden()
    page.click("#bed-merge-toggle")
    target = page.locator("#bed-merge-target")
    assert target.locator("option:checked").inner_text() == "Raised bed 1", "the bed this name starts with is preselected"
    assert page.input_value("#bed-merge-spot") == "north end"
    target.select_option(label="Herb pot")
    assert page.input_value("#bed-merge-spot") == "Raised bed 1 - north end"
    target.select_option(label="Raised bed 1")
    page.fill("#bed-merge-spot", "north end, by the fence")
    target.select_option(label="Herb pot")
    target.select_option(label="Raised bed 1")
    assert page.input_value("#bed-merge-spot") == "north end, by the fence", "typed text isn't overwritten"

    page.evaluate("window.confirm = () => true")
    with page.expect_navigation():
        page.click("#bed-merge-form button[type=submit]")
    assert page.inner_text("h2") == "Raised bed 1"
    assert "north end, by the fence" in page.locator(".bed-row", has_text="Peas").inner_text()
    row = db.one("SELECT location_name, bed_spot FROM garden_entries WHERE plant_name = 'Peas'")
    assert row == {"location_name": "Raised bed 1", "bed_spot": "north end, by the fence"}
    assert [r["name"] for r in db.all("SELECT name FROM yard_beds ORDER BY name")] == ["Herb pot", "Raised bed 1"]


def test_spot_in_bed_field_on_edible_form(logged_in, db):
    page = logged_in
    page.goto("/garden/new")
    page.fill("input[name=plant_name]", "Garlic")
    page.fill("#loc-name-input", "Long bed")
    page.fill("#bed-spot-input", "south corner")
    with page.expect_navigation():
        page.click("button:has-text('Save to Edibles')")
    assert "south corner" in page.inner_text("#entry-bed-spot")
    entry = db.one("SELECT id FROM garden_entries")
    page.goto(f"/garden/{entry['id']}/edit")
    assert page.input_value("#bed-spot-input") == "south corner"
