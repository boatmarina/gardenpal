"""Beds / containers inside yard zones."""

import re

from tests.conftest import signup
from tests.test_ai_actions import fake_ai  # noqa: F401 (fixture)
from tests.test_garden import make_entry


def make_zone(client, db, name="Middle North"):
    client.post("/yard/zones/new", data={"name": name})
    return db.one("SELECT * FROM yard_zones WHERE name = ? ORDER BY id DESC", (name,))


def add_bed(client, db, zone, name):
    resp = client.post(f"/yard/zones/{zone['id']}/beds", data={"name": name})
    assert resp.status_code == 302, resp.data[:300]
    return db.one("SELECT * FROM yard_beds WHERE zone_id = ? AND lower(name) = lower(?)", (zone["id"], name))


def bed_names(db, zone):
    return [r["name"] for r in db.all("SELECT name FROM yard_beds WHERE zone_id = ? ORDER BY name", (zone["id"],))]


class TestBedsOnZonePage:
    def test_existing_location_names_show_up_as_beds(self, user_client, db):
        make_entry(user_client, db, "Peas", zone_name="Middle North", location_name="Peas bed")
        make_entry(user_client, db, "Leeks", zone_name="Middle North", location_name=" peas BED ")
        make_entry(user_client, db, "Basil", zone_name="Back Deck", location_name="Basil planter")
        zone = db.one("SELECT * FROM yard_zones WHERE name = 'Middle North'")

        page = user_client.get(f"/yard/zones/{zone['id']}").data.decode()
        assert bed_names(db, zone) == ["Peas bed"], "same name in different case/spacing is one bed"
        assert "Peas bed" in page and "2 plants" in page
        assert "Basil planter" not in page, "beds from other zones must not appear"

    def test_add_bed_and_reject_bad_names(self, user_client, db):
        zone = make_zone(user_client, db)
        bed = add_bed(user_client, db, zone, "Raised bed 1")
        assert bed["user_id"] == user_client.user["id"]
        assert db.one("SELECT 1 AS x FROM activity_log WHERE action = 'bed_added'")

        dup = user_client.post(f"/yard/zones/{zone['id']}/beds", data={"name": "raised BED 1"})
        assert dup.headers["Location"].endswith(f"/yard/beds/{bed['id']}")
        user_client.post(f"/yard/zones/{zone['id']}/beds", data={"name": "   "})
        user_client.post(f"/yard/zones/{zone['id']}/beds", data={"name": "x" * 81})
        assert bed_names(db, zone) == ["Raised bed 1"]

    def test_differently_spelled_bed_keeps_earliest_spelling(self, user_client, db):
        # The earliest-planted spelling wins, even when it was inserted last (table order differs).
        for j in range(4):
            make_entry(user_client, db, f"Leeks {j}", zone_name="Middle North", location_name=" peas BED ")
        first = make_entry(user_client, db, "Peas", zone_name="Middle North", location_name="Peas bed")
        db.run("UPDATE garden_entries SET created_at = '2000-01-01T00:00:00' WHERE id = ?", (first["id"],))
        zone = db.one("SELECT * FROM yard_zones WHERE name = 'Middle North'")
        user_client.get(f"/yard/zones/{zone['id']}")
        assert bed_names(db, zone) == ["Peas bed"]

    def test_filter_chips_and_card_tags(self, user_client, db):
        make_entry(user_client, db, "Peas", zone_name="Middle North", location_name="Peas bed")
        make_entry(user_client, db, "Kale", zone_name="Middle North")
        zone = db.one("SELECT * FROM yard_zones WHERE name = 'Middle North'")
        add_bed(user_client, db, zone, "Empty pot")
        page = user_client.get(f"/yard/zones/{zone['id']}").data.decode()
        chips = re.findall(r'class="bed-chip" data-bed="([^"]*)"', page)
        assert chips == ["__all", "peas bed", "__none"], "empty beds don't get a filter chip"
        assert 'data-bed="peas bed"' in page and 'data-bed=""' in page


class TestBedPage:
    def test_details_plants_and_notes(self, user_client, db):
        entry = make_entry(user_client, db, "Peas", zone_name="Middle North", location_name="Peas bed")
        zone = db.one("SELECT * FROM yard_zones WHERE id = ?", (entry["zone_id"],))
        user_client.get(f"/yard/zones/{zone['id']}")
        bed = db.one("SELECT * FROM yard_beds")

        page = user_client.get(f"/yard/beds/{bed['id']}").data.decode()
        assert "Peas bed" in page and "Peas" in page and f"/garden/{entry['id']}" in page

        user_client.post(f"/yard/beds/{bed['id']}/edit",
                         data={"name": "Peas bed", "sun_exposure": "part_sun", "soil_notes": "Added 2in compost"})
        row = db.one("SELECT sun_exposure, soil_notes FROM yard_beds WHERE id = ?", (bed["id"],))
        assert row == {"sun_exposure": "part_sun", "soil_notes": "Added 2in compost"}

        user_client.post(f"/yard/beds/{bed['id']}/notes", data={"notes": "Soil test: pH 6.5", "note_date": "2026-10-01"})
        user_client.post(f"/yard/beds/{bed['id']}/notes", data={"notes": "  "})
        notes = db.all("SELECT * FROM yard_bed_notes")
        assert [n["notes"] for n in notes] == ["Soil test: pH 6.5"] and notes[0]["note_date"] == "2026-10-01"
        page = user_client.get(f"/yard/beds/{bed['id']}").data.decode()
        assert "Part sun" in page and "Added 2in compost" in page and "Soil test: pH 6.5" in page

        user_client.post(f"/yard/bed-notes/{notes[0]['id']}/delete")
        assert db.one("SELECT 1 AS x FROM yard_bed_notes") is None
        actions = {r["action"] for r in db.all("SELECT action FROM activity_log")}
        assert {"bed_updated", "bed_note_added"} <= actions

    def test_unknown_sun_value_is_ignored(self, user_client, db):
        zone = make_zone(user_client, db)
        bed = add_bed(user_client, db, zone, "Pot")
        user_client.post(f"/yard/beds/{bed['id']}/edit", data={"name": "Pot", "sun_exposure": "blazing"})
        assert db.one("SELECT sun_exposure FROM yard_beds")["sun_exposure"] is None

    def test_rename_moves_plants_in_that_zone_only(self, user_client, db):
        a = make_entry(user_client, db, "Peas", zone_name="North", location_name="Bed A")
        b = make_entry(user_client, db, "Beans", zone_name="South", location_name="Bed A")
        north = db.one("SELECT * FROM yard_zones WHERE name = 'North'")
        user_client.get(f"/yard/zones/{north['id']}")
        bed = db.one("SELECT * FROM yard_beds WHERE zone_id = ?", (north["id"],))
        user_client.post(f"/yard/beds/{bed['id']}/edit", data={"name": "Pea trellis"})
        loc = {r["id"]: r["location_name"] for r in db.all("SELECT id, location_name FROM garden_entries")}
        assert loc == {a["id"]: "Pea trellis", b["id"]: "Bed A"}

    def test_rename_cannot_clash_with_another_bed(self, user_client, db):
        zone = make_zone(user_client, db)
        add_bed(user_client, db, zone, "Bed A")
        b = add_bed(user_client, db, zone, "Bed B")
        user_client.post(f"/yard/beds/{b['id']}/edit", data={"name": "bed a"})
        assert bed_names(db, zone) == ["Bed A", "Bed B"]

    def test_delete_only_when_empty(self, user_client, db):
        make_entry(user_client, db, "Peas", zone_name="North", location_name="Bed A")
        zone = db.one("SELECT * FROM yard_zones WHERE name = 'North'")
        user_client.get(f"/yard/zones/{zone['id']}")
        used = db.one("SELECT * FROM yard_beds")
        user_client.post(f"/yard/beds/{used['id']}/delete")
        assert db.one("SELECT 1 AS x FROM yard_beds WHERE id = ?", (used["id"],))

        empty = add_bed(user_client, db, zone, "Spare pot")
        user_client.post(f"/yard/beds/{empty['id']}/notes", data={"notes": "cracked"})
        user_client.post(f"/yard/beds/{empty['id']}/delete")
        assert db.one("SELECT 1 AS x FROM yard_beds WHERE id = ?", (empty["id"],)) is None
        assert db.one("SELECT 1 AS x FROM yard_bed_notes") is None


class TestBedAccess:
    def test_other_user_cannot_see_or_change_beds(self, app, user_client, db):
        zone = make_zone(user_client, db)
        bed = add_bed(user_client, db, zone, "Secret bed")
        user_client.post(f"/yard/beds/{bed['id']}/notes", data={"notes": "private"})
        note = db.one("SELECT id FROM yard_bed_notes")

        intruder = app.test_client()
        signup(intruder, "intruder")
        assert b"Secret bed" not in intruder.get(f"/yard/beds/{bed['id']}", follow_redirects=True).data
        intruder.post(f"/yard/zones/{zone['id']}/beds", data={"name": "Planted by intruder"})
        intruder.post(f"/yard/beds/{bed['id']}/edit", data={"name": "Hacked", "soil_notes": "x"})
        intruder.post(f"/yard/beds/{bed['id']}/notes", data={"notes": "hi"})
        intruder.post(f"/yard/bed-notes/{note['id']}/delete")
        intruder.post(f"/yard/beds/{bed['id']}/delete")

        assert bed_names(db, zone) == ["Secret bed"]
        assert db.one("SELECT soil_notes FROM yard_beds")["soil_notes"] is None
        assert [n["notes"] for n in db.all("SELECT notes FROM yard_bed_notes")] == ["private"]

    def test_garden_partner_can_use_beds(self, app, db):
        alice = app.test_client()
        signup(alice, "alice")
        bob = app.test_client()
        signup(bob, "bob")
        alice.post("/settings/share-garden", data={"partner_username": "bob"})
        bob.post("/settings/share-garden", data={"partner_username": "alice"})
        zone = make_zone(alice, db)
        bed = add_bed(alice, db, zone, "Shared bed")
        assert b"Shared bed" in bob.get(f"/yard/beds/{bed['id']}").data
        bob.post(f"/yard/beds/{bed['id']}/notes", data={"notes": "watered deeply"})
        assert db.one("SELECT notes FROM yard_bed_notes")["notes"] == "watered deeply"


class TestBedsInAiContext:
    def _bed_with_details(self, client, db):
        entry = make_entry(client, db, "Peas", zone_name="Middle North", location_name="Peas bed")
        client.get(f"/yard/zones/{entry['zone_id']}")
        bed = db.one("SELECT * FROM yard_beds")
        client.post(f"/yard/beds/{bed['id']}/edit",
                    data={"name": "Peas bed", "sun_exposure": "shade", "soil_notes": "Heavy clay"})
        client.post(f"/yard/beds/{bed['id']}/notes", data={"notes": "Added gypsum", "note_date": "2026-09-01"})
        return entry

    def test_plant_chat_gets_its_bed_details(self, user_client, db, fake_ai):
        entry = self._bed_with_details(user_client, db)
        user_client.post("/api/entry-chat", json={"message": "why so slow?", "entry_id": entry["id"]})
        system = fake_ai.calls[0]["system"]
        assert "Peas bed" in system and "Shade" in system and "Heavy clay" in system and "Added gypsum" in system

    def test_garden_wide_chats_list_beds(self, user_client, db, fake_ai):
        self._bed_with_details(user_client, db)
        user_client.post("/api/garden-chat", json={"message": "how are my beds?"})
        user_client.post("/api/home-chat", json={"message": "how are my beds?"})
        for call in fake_ai.calls:
            assert "Peas bed (zone Middle North)" in call["system"] and "Heavy clay" in call["system"]


class TestAdminAndWhatsNew:
    def test_admin_sees_bed_activity(self, user_client, db):
        db.run("UPDATE users SET is_admin = 1 WHERE id = ?", (user_client.user["id"],))
        zone = make_zone(user_client, db)
        add_bed(user_client, db, zone, "Herb pot")
        page = user_client.get("/tools").data.decode()
        assert "Beds &amp; containers activity" in page and "Herb pot (Middle North)" in page

    def test_draft_entries_never_count_as_seen(self, app, user_client, db):
        import gardenpal.web as web  # noqa: F401
        seen = db.one("SELECT whats_new_seen FROM users WHERE id = ?", (user_client.user["id"],))["whats_new_seen"]
        assert seen == "2026-09-a", "signup should record the newest published entry, not a draft"
        page = user_client.get("/").data.decode()
        assert "Track the beds and containers" not in page, "draft entries must not be shown"


def add_ornamental(client, db, zone, name="Lavender", **extra):
    resp = client.post("/yard/plants/new", data={"zone_id": zone["id"], "plant_name": name, **extra})
    assert resp.status_code == 302, resp.data[:300]
    return db.one("SELECT * FROM yard_plants WHERE plant_name = ? ORDER BY id DESC", (name,))


class TestBedTypeAndSize:
    def test_edit_type_and_size(self, user_client, db):
        zone = make_zone(user_client, db)
        bed = add_bed(user_client, db, zone, "Herb pot")
        user_client.post(f"/yard/beds/{bed['id']}/edit",
                         data={"name": "Herb pot", "bed_type": "container", "size": "15 gal"})
        assert db.one("SELECT bed_type, size FROM yard_beds") == {"bed_type": "container", "size": "15 gal"}
        page = user_client.get(f"/yard/beds/{bed['id']}").data.decode()
        assert "Container · 15 gal" in page
        assert "Container" in user_client.get(f"/yard/zones/{zone['id']}").data.decode()

        user_client.post(f"/yard/beds/{bed['id']}/edit", data={"name": "Herb pot", "bed_type": "castle", "size": "x" * 200})
        row = db.one("SELECT bed_type, size FROM yard_beds")
        assert row["bed_type"] == "container" and len(row["size"]) == 60


class TestOrnamentalsInBeds:
    def test_set_bed_from_plant_page_including_new_and_clear(self, user_client, db):
        zone = make_zone(user_client, db)
        bed = add_bed(user_client, db, zone, "Front pots")
        yp = add_ornamental(user_client, db, zone)
        lib = db.one("SELECT id FROM plants WHERE name = 'Lavender'")

        resp = user_client.post(f"/yard/plants/{yp['id']}/bed", data={"bed_choice": str(bed["id"]), "plant_id": lib["id"]})
        assert resp.headers["Location"].endswith(f"/ideas/{lib['id']}")
        assert db.one("SELECT bed_id FROM yard_plants")["bed_id"] == bed["id"]

        user_client.post(f"/yard/plants/{yp['id']}/bed", data={"bed_choice": "__new", "new_bed_name": "Window box"})
        new_bed = db.one("SELECT id FROM yard_beds WHERE name = 'Window box'")
        assert new_bed and db.one("SELECT bed_id FROM yard_plants")["bed_id"] == new_bed["id"]

        user_client.post(f"/yard/plants/{yp['id']}/bed", data={"bed_choice": ""})
        assert db.one("SELECT bed_id FROM yard_plants")["bed_id"] is None

    def test_cannot_use_a_bed_from_another_zone(self, user_client, db):
        zone = make_zone(user_client, db, "North")
        other = make_zone(user_client, db, "South")
        far_bed = add_bed(user_client, db, other, "South bed")
        yp = add_ornamental(user_client, db, zone)
        user_client.post(f"/yard/plants/{yp['id']}/bed", data={"bed_choice": str(far_bed["id"])})
        assert db.one("SELECT bed_id FROM yard_plants")["bed_id"] is None

    def test_choose_bed_when_adding_from_yard(self, user_client, db):
        zone = make_zone(user_client, db)
        bed = add_bed(user_client, db, zone, "Front pots")
        add_ornamental(user_client, db, zone, "Lavender", bed_choice=str(bed["id"]))
        add_ornamental(user_client, db, zone, "Salvia", bed_choice="__new", new_bed_name="Border")
        add_ornamental(user_client, db, zone, "Hosta")
        beds = {r["plant_name"]: r["bed"] for r in db.all(
            "SELECT yp.plant_name, b.name AS bed FROM yard_plants yp LEFT JOIN yard_beds b ON b.id = yp.bed_id")}
        assert beds == {"Lavender": "Front pots", "Salvia": "Border", "Hosta": None}

    def test_bed_page_zone_filter_and_delete_count_ornamentals(self, user_client, db):
        zone = make_zone(user_client, db)
        bed = add_bed(user_client, db, zone, "Front pots")
        add_ornamental(user_client, db, zone, "Lavender", bed_choice=str(bed["id"]))
        assert "Lavender" in user_client.get(f"/yard/beds/{bed['id']}").data.decode()
        zone_page = user_client.get(f"/yard/zones/{zone['id']}").data.decode()
        assert 'data-bed="front pots"' in zone_page and "1 plant" in zone_page
        user_client.post(f"/yard/beds/{bed['id']}/delete")
        assert db.one("SELECT 1 AS x FROM yard_beds WHERE id = ?", (bed["id"],)), "bed with an ornamental can't be deleted"

    def test_other_user_cannot_set_bed(self, app, user_client, db):
        zone = make_zone(user_client, db)
        bed = add_bed(user_client, db, zone, "Front pots")
        yp = add_ornamental(user_client, db, zone)
        intruder = app.test_client()
        signup(intruder, "intruder")
        intruder.post(f"/yard/plants/{yp['id']}/bed", data={"bed_choice": str(bed["id"])})
        assert db.one("SELECT bed_id FROM yard_plants")["bed_id"] is None

    def test_ornamental_page_shows_bed_notes(self, user_client, db):
        zone = make_zone(user_client, db)
        bed = add_bed(user_client, db, zone, "Front pots")
        add_ornamental(user_client, db, zone, "Lavender", bed_choice=str(bed["id"]))
        user_client.post(f"/yard/beds/{bed['id']}/notes", data={"notes": "Repotted with grit", "note_date": "2026-04-01"})
        lib = db.one("SELECT id FROM plants WHERE name = 'Lavender'")
        page = user_client.get(f"/ideas/{lib['id']}").data.decode()
        assert "Bed note · Front pots" in page and "Repotted with grit" in page


class TestBedNotesInGrowthLog:
    def _setup(self, client, db):
        entry = make_entry(client, db, "Peas", zone_name="North", location_name="Bed A")
        client.get(f"/yard/zones/{entry['zone_id']}")
        bed = db.one("SELECT * FROM yard_beds")
        for date, text in (("2026-03-01", "Added compost"), ("2026-08-01", "Mulched"), ("2026-10-01", "Pulled everything")):
            client.post(f"/yard/beds/{bed['id']}/notes", data={"notes": text, "note_date": date})
        return entry, bed

    def test_bed_notes_show_in_growth_log_in_date_order(self, user_client, db):
        entry, bed = self._setup(user_client, db)
        user_client.post(f"/garden/{entry['id']}/photos",
                         data={"photo_notes": "First flowers", "photo_date": "2026-06-15"},
                         headers={"X-Requested-With": "XMLHttpRequest"})
        page = user_client.get(f"/garden/{entry['id']}").data.decode()
        order = [page.index(t) for t in ("Pulled everything", "Mulched", "First flowers", "Added compost")]
        assert order == sorted(order), "newest first, bed notes interleaved with the plant's own notes"
        assert page.count("Bed note · Bed A") == 3

    def test_notes_after_done_for_season_are_left_out(self, user_client, db):
        entry, bed = self._setup(user_client, db)
        user_client.post(f"/garden/{entry['id']}/toggle-done", headers={"X-Requested-With": "XMLHttpRequest"})
        assert db.one("SELECT done_for_season_at FROM garden_entries")["done_for_season_at"]
        db.run("UPDATE garden_entries SET done_for_season_at = '2026-09-01'")
        page = user_client.get(f"/garden/{entry['id']}").data.decode()
        assert "Added compost" in page and "Mulched" in page
        assert "Pulled everything" not in page

        user_client.post(f"/garden/{entry['id']}/toggle-done", headers={"X-Requested-With": "XMLHttpRequest"})
        assert db.one("SELECT done_for_season_at FROM garden_entries")["done_for_season_at"] is None
        assert "Pulled everything" in user_client.get(f"/garden/{entry['id']}").data.decode()

    def test_backfills_done_date_for_plants_marked_done_earlier(self, app, user_client, db):
        from gardenpal.web import init_db
        entry = make_entry(user_client, db, "Peas")
        db.run("UPDATE garden_entries SET done_for_season = 1, done_for_season_at = NULL WHERE id = ?", (entry["id"],))
        db.run("INSERT INTO activity_log (user_id, action, item_name, logged_at) VALUES (?, 'garden_done_season', 'Peas', '2026-09-12T10:00:00')",
               (user_client.user["id"],))
        with app.app_context():
            init_db()
        assert db.one("SELECT done_for_season_at FROM garden_entries")["done_for_season_at"] == "2026-09-12"


class TestEdibleBedPicker:
    def test_forms_get_beds_for_each_zone(self, user_client, db):
        entry = make_entry(user_client, db, "Peas", zone_name="North", location_name="Bed A")
        zone = db.one("SELECT * FROM yard_zones")
        bed = add_bed(user_client, db, zone, "Bed B")
        user_client.post(f"/yard/beds/{bed['id']}/edit", data={"name": "Bed B", "bed_type": "raised_bed"})
        for path in ("/garden/new", f"/garden/{entry['id']}/edit", f"/garden/{entry['id']}/duplicate"):
            page = user_client.get(path).data.decode()
            m = re.search(r"data-zone-beds='([^']*)'", page)
            assert m, path
            assert '"name": "Bed A"' in m.group(1) and '"type": "raised_bed"' in m.group(1), path
