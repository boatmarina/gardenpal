"""Edibles (garden entries) and their growth-log notes."""

import io

from PIL import Image

from tests.conftest import signup

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def make_entry(client, db, name="Tomato", **fields):
    data = {"plant_name": name, "planted_date": "2026-05-01", **fields}
    resp = client.post("/garden/new", data=data)
    assert resp.status_code == 302, resp.data[:500]
    return db.one("SELECT * FROM garden_entries WHERE plant_name = ? ORDER BY id DESC", (name,))


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (90, 140, 60)).save(buf, "PNG")
    buf.seek(0)
    return buf


class TestEntries:
    def test_create_entry_and_view_detail(self, user_client, db):
        entry = make_entry(user_client, db, "Leeks", variety="Autumn Giant", location_name="Raised bed")
        assert entry["variety"] == "Autumn Giant"
        page = user_client.get(f"/garden/{entry['id']}")
        assert page.status_code == 200
        assert b"Leeks" in page.data and b"Autumn Giant" in page.data
        assert b"/garden" in user_client.get("/garden").data

    def test_create_requires_plant_name(self, user_client, db):
        resp = user_client.post("/garden/new", data={"plant_name": "  "})
        assert resp.status_code == 200
        assert b"Plant name is required" in resp.data
        assert db.one("SELECT 1 AS x FROM garden_entries") is None

    def test_create_with_new_zone_name_creates_zone(self, user_client, db):
        entry = make_entry(user_client, db, "Peas", zone_name="Middle North")
        zone = db.one("SELECT * FROM yard_zones WHERE name = 'Middle North'")
        assert zone and entry["zone_id"] == zone["id"]
        assert b"Middle North" in user_client.get(f"/garden/{entry['id']}").data

    def test_edit_entry(self, user_client, db):
        entry = make_entry(user_client, db, "Zucchni")
        resp = user_client.post(f"/garden/{entry['id']}/edit", data={"plant_name": "Zucchini", "variety": "Black Beauty"})
        assert resp.status_code == 302
        row = db.one("SELECT plant_name, variety FROM garden_entries WHERE id = ?", (entry["id"],))
        assert row == {"plant_name": "Zucchini", "variety": "Black Beauty"}

    def test_duplicate_prefills_form(self, user_client, db):
        entry = make_entry(user_client, db, "Basil", variety="Genovese")
        resp = user_client.get(f"/garden/{entry['id']}/duplicate")
        assert resp.status_code == 200
        assert b'value="Basil"' in resp.data and b"Genovese" in resp.data

    def test_toggle_done_for_season(self, user_client, db):
        entry = make_entry(user_client, db)
        first = user_client.post(f"/garden/{entry['id']}/toggle-done", headers=AJAX).get_json()
        assert first == {"ok": True, "done_for_season": 1}
        second = user_client.post(f"/garden/{entry['id']}/toggle-done", headers=AJAX).get_json()
        assert second["done_for_season"] == 0

    def test_delete_entry_removes_notes(self, user_client, db):
        entry = make_entry(user_client, db)
        user_client.post(f"/garden/{entry['id']}/photos", data={"photo_notes": "first flower"}, headers=AJAX)
        resp = user_client.post(f"/garden/{entry['id']}/delete")
        assert resp.status_code == 302
        assert db.one("SELECT 1 AS x FROM garden_entries WHERE id = ?", (entry["id"],)) is None
        assert db.one("SELECT 1 AS x FROM garden_photos WHERE entry_id = ?", (entry["id"],)) is None

    def test_special_characters_are_escaped(self, user_client, db):
        entry = make_entry(user_client, db, "Beans & Peas <script>alert(1)</script>")
        page = user_client.get(f"/garden/{entry['id']}").data
        assert b"<script>alert(1)</script>" not in page
        assert b"Beans &amp; Peas" in page

    def test_very_long_name_is_accepted(self, user_client, db):
        name = "Cherry tomato " * 30
        entry = make_entry(user_client, db, name.strip())
        assert user_client.get(f"/garden/{entry['id']}").status_code == 200


class TestGrowthLogNotes:
    """The add-note route is called via fetch() and must always answer with JSON."""

    def test_add_text_note_ajax_returns_entry_data(self, user_client, db):
        entry = make_entry(user_client, db, "Leeks")
        resp = user_client.post(
            f"/garden/{entry['id']}/photos",
            data={"photo_notes": "Got crowded out by zucchini", "photo_date": "2026-10-22"},
            headers=AJAX,
        )
        assert resp.status_code == 200
        assert resp.is_json, "client calls res.json(); an HTML error page shows 'Save failed'"
        data = resp.get_json()
        row = db.one("SELECT * FROM garden_photos WHERE entry_id = ?", (entry["id"],))
        assert data == {
            "ok": True,
            "photo_id": row["id"],
            "photo_date": "2026-10-22",
            "note_text": "Got crowded out by zucchini",
            "photo_url": None,
        }

    def test_add_photo_note_returns_servable_url(self, user_client, db):
        entry = make_entry(user_client, db)
        resp = user_client.post(
            f"/garden/{entry['id']}/photos",
            data={"photo": (png_bytes(), "leaf.png"), "photo_date": "2026-10-01"},
            headers=AJAX,
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        assert data["ok"] and data["photo_url"]
        assert user_client.get(data["photo_url"]).status_code == 200
        assert data["photo_url"].encode() in user_client.get(f"/garden/{entry['id']}").data

    def test_empty_note_is_rejected_with_json(self, user_client, db):
        entry = make_entry(user_client, db)
        resp = user_client.post(f"/garden/{entry['id']}/photos", data={"photo_notes": "  "}, headers=AJAX)
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_non_ajax_note_redirects_back_to_entry(self, user_client, db):
        entry = make_entry(user_client, db)
        resp = user_client.post(f"/garden/{entry['id']}/photos", data={"photo_notes": "watered deeply"})
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith(f"/garden/{entry['id']}")

    def test_note_mentioning_fertilizer_is_logged_as_fertilization(self, user_client, db):
        entry = make_entry(user_client, db)
        user_client.post(
            f"/garden/{entry['id']}/photos",
            data={"photo_notes": "Fertilized with fish emulsion", "photo_date": "2026-07-01"},
            headers=AJAX,
        )
        row = db.one("SELECT is_fertilization FROM garden_photos WHERE entry_id = ?", (entry["id"],))
        assert row["is_fertilization"] == 1

    def test_notes_show_on_detail_page_and_escape_html(self, user_client, db):
        entry = make_entry(user_client, db)
        user_client.post(f"/garden/{entry['id']}/photos", data={"photo_notes": "<b>huge</b> & healthy"}, headers=AJAX)
        page = user_client.get(f"/garden/{entry['id']}").data
        assert b"&lt;b&gt;huge&lt;/b&gt; &amp; healthy" in page

    def test_edit_and_delete_note(self, user_client, db):
        entry = make_entry(user_client, db)
        photo_id = user_client.post(
            f"/garden/{entry['id']}/photos", data={"photo_notes": "first"}, headers=AJAX
        ).get_json()["photo_id"]

        assert user_client.get(f"/garden/photos/{photo_id}/edit").status_code == 200
        user_client.post(f"/garden/photos/{photo_id}/edit", data={"notes": "edited", "photo_date": "2026-08-01"})
        assert db.one("SELECT notes FROM garden_photos WHERE id = ?", (photo_id,))["notes"] == "edited"

        user_client.post(f"/garden/photos/{photo_id}/delete")
        assert db.one("SELECT 1 AS x FROM garden_photos WHERE id = ?", (photo_id,)) is None


class TestCare:
    def test_watered_today(self, user_client, db):
        entry = make_entry(user_client, db)
        data = user_client.post(f"/garden/{entry['id']}/watered-today", headers=AJAX).get_json()
        assert data["ok"] is True and data["today"]
        row = db.one("SELECT last_watered_date FROM garden_entries WHERE id = ?", (entry["id"],))
        assert row["last_watered_date"] == data["today"]
        assert db.one("SELECT action FROM activity_log WHERE action = 'watered'")

    def test_fertilized_today_adds_growth_note(self, user_client, db):
        entry = make_entry(user_client, db)
        resp = user_client.post(
            f"/garden/{entry['id']}/fertilized-today", data={"fertilizer_type": "Worm castings"}, headers=AJAX
        )
        data = resp.get_json()
        assert data["ok"] is True and data["photo_id"]
        note = db.one("SELECT notes, is_fertilization FROM garden_photos WHERE id = ?", (data["photo_id"],))
        assert note == {"notes": "Fertilized — Worm castings", "is_fertilization": 1}

    def test_log_fertilization_validates_date(self, user_client, db):
        entry = make_entry(user_client, db)
        bad = user_client.post(f"/garden/{entry['id']}/log-fertilization", data={"date": "yesterday"}, headers=AJAX)
        assert bad.status_code == 400
        ok = user_client.post(f"/garden/{entry['id']}/log-fertilization", data={"date": "2026-09-01"}, headers=AJAX)
        assert ok.get_json()["ok"] is True

    def test_mark_all_watered(self, user_client, db):
        a = make_entry(user_client, db, "Kale")
        b = make_entry(user_client, db, "Chard")
        resp = user_client.post("/watering/mark-all-today", data={"item": [f"edible:{a['id']}", f"edible:{b['id']}", "junk"]})
        assert resp.status_code == 302
        rows = db.all("SELECT last_watered_date FROM garden_entries")
        assert all(r["last_watered_date"] for r in rows)


class TestIsolation:
    """One user's garden must never be readable or writable by another user."""

    def test_other_user_cannot_view_edit_or_delete(self, app, user_client, db):
        entry = make_entry(user_client, db, "Secret squash")
        intruder = app.test_client()
        signup(intruder, "intruder")

        assert b"Secret squash" not in intruder.get("/garden").data
        assert intruder.get(f"/garden/{entry['id']}").status_code in (302, 404)

        resp = intruder.post(f"/garden/{entry['id']}/photos", data={"photo_notes": "hi"}, headers=AJAX)
        assert resp.status_code == 404
        intruder.post(f"/garden/{entry['id']}/edit", data={"plant_name": "Hacked"})
        intruder.post(f"/garden/{entry['id']}/delete")
        intruder.post(f"/garden/{entry['id']}/toggle-done", headers=AJAX)

        row = db.one("SELECT plant_name, done_for_season FROM garden_entries WHERE id = ?", (entry["id"],))
        assert row["plant_name"] == "Secret squash"
        assert not row["done_for_season"]
        assert db.one("SELECT 1 AS x FROM garden_photos") is None
