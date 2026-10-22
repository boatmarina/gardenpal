"""Ornamentals library, Yard zones, Settings, and shared gardens."""

from tests.conftest import signup
from tests.test_garden import AJAX, make_entry


def make_plant(client, db, name="Coneflower", **fields):
    data = {"name": name, "lookup_status": "draft", **fields}
    resp = client.post("/ideas/new", data=data)
    assert resp.status_code == 302, resp.data[:500]
    return db.one("SELECT * FROM plants WHERE name = ? ORDER BY id DESC", (name,))


def make_zone(client, db, name="Front Bed"):
    resp = client.post("/yard/zones/new", data={"name": name, "description": "sunny"})
    assert resp.status_code == 302, resp.data[:500]
    return db.one("SELECT * FROM yard_zones WHERE name = ? ORDER BY id DESC", (name,))


class TestLibrary:
    def test_add_by_name_and_view(self, user_client, db):
        plant = make_plant(user_client, db, "Purple Coneflower", scientific_name="Echinacea purpurea")
        assert user_client.get(f"/ideas/{plant['id']}").status_code == 200
        assert b"Purple Coneflower" in user_client.get("/ideas").data

    def test_add_requires_name(self, user_client, db):
        resp = user_client.post("/ideas/new", data={"name": "", "lookup_status": "draft"})
        assert resp.status_code == 200
        assert b"Plant name is required" in resp.data
        assert db.one("SELECT 1 AS x FROM plants") is None

    def test_add_still_saves_when_lookup_services_are_down(self, user_client, db):
        resp = user_client.post("/ideas/new", data={"name": "Hosta"})
        assert resp.status_code == 302
        assert db.one("SELECT name FROM plants WHERE name = 'Hosta'")

    def test_add_with_tags_then_add_and_remove_tag(self, user_client, db):
        plant = make_plant(user_client, db, tag_names="Shade, Native")
        names = {r["name"] for r in db.all(
            "SELECT t.name FROM tags t JOIN plant_tags pt ON pt.tag_id = t.id WHERE pt.plant_id = ?", (plant["id"],))}
        assert names == {"Shade", "Native"}

        tag = user_client.post(f"/ideas/{plant['id']}/tags", data={"tag_name": "Pollinator"}, headers=AJAX).get_json()
        assert tag["name"] == "Pollinator"
        resp = user_client.post(f"/ideas/{plant['id']}/tags/{tag['id']}/remove", headers=AJAX)
        assert resp.get_json() == {"ok": True}

    def test_plant_notes(self, user_client, db):
        plant = make_plant(user_client, db)
        user_client.post(f"/ideas/{plant['id']}/add-note", data={"notes": "Deadheaded today", "note_date": "2026-08-01"})
        note = db.one("SELECT * FROM plant_notes WHERE plant_id = ?", (plant["id"],))
        assert note["notes"] == "Deadheaded today"
        assert b"Deadheaded today" in user_client.get(f"/ideas/{plant['id']}").data
        user_client.post(f"/ideas/notes/{note['id']}/delete")
        assert db.one("SELECT 1 AS x FROM plant_notes") is None

    def test_add_to_zone(self, user_client, db):
        plant = make_plant(user_client, db, "Hydrangea")
        zone = make_zone(user_client, db)
        user_client.post(f"/ideas/{plant['id']}/add-to-zone", data={"zone_id": zone["id"]})
        assert db.one("SELECT plant_name FROM yard_plants WHERE zone_id = ?", (zone["id"],))["plant_name"] == "Hydrangea"
        assert b"Hydrangea" in user_client.get(f"/yard/zones/{zone['id']}").data

    def test_watered_and_fertilized_today(self, user_client, db):
        plant = make_plant(user_client, db)
        assert user_client.post(f"/ideas/{plant['id']}/watered-today").get_json()["ok"] is True
        assert user_client.post(f"/ideas/{plant['id']}/fertilized-today").get_json()["ok"] is True

    def test_delete(self, user_client, db):
        plant = make_plant(user_client, db, tag_names="Shade")
        assert user_client.post(f"/ideas/{plant['id']}/delete").status_code == 302
        assert db.one("SELECT 1 AS x FROM plants WHERE id = ?", (plant["id"],)) is None

    def test_other_user_cannot_view_or_delete(self, app, user_client, db):
        plant = make_plant(user_client, db, "Private peony")
        intruder = app.test_client()
        signup(intruder, "intruder")
        assert b"Private peony" not in intruder.get(f"/ideas/{plant['id']}", follow_redirects=True).data
        intruder.post(f"/ideas/{plant['id']}/delete")
        assert db.one("SELECT 1 AS x FROM plants WHERE id = ?", (plant["id"],))


class TestYard:
    def test_create_edit_and_view_zone(self, user_client, db):
        zone = make_zone(user_client, db, "Back Border")
        assert b"Back Border" in user_client.get("/yard").data
        user_client.post(f"/yard/zones/{zone['id']}/edit", data={"name": "Back Border North", "description": ""})
        assert db.one("SELECT name FROM yard_zones WHERE id = ?", (zone["id"],))["name"] == "Back Border North"
        assert user_client.get(f"/yard/zones/{zone['id']}").status_code == 200

    def test_zone_requires_name(self, user_client, db):
        resp = user_client.post("/yard/zones/new", data={"name": ""})
        assert resp.status_code == 200
        assert b"Zone name is required" in resp.data

    def test_add_yard_plant_also_creates_library_plant(self, user_client, db):
        zone = make_zone(user_client, db)
        resp = user_client.post("/yard/plants/new", data={"zone_id": zone["id"], "plant_name": "Lavender"})
        assert resp.status_code == 302
        yp = db.one("SELECT * FROM yard_plants WHERE plant_name = 'Lavender'")
        assert yp and yp["zone_id"] == zone["id"]
        assert db.one("SELECT 1 AS x FROM plants WHERE name = 'Lavender'")

        user_client.post(f"/yard/plants/{yp['id']}/notes", data={"notes": "Pruned back"})
        assert db.one("SELECT notes FROM yard_plant_notes WHERE yard_plant_id = ?", (yp["id"],))["notes"] == "Pruned back"

        user_client.post(f"/yard/plants/{yp['id']}/remove")
        assert db.one("SELECT 1 AS x FROM yard_plants WHERE id = ?", (yp["id"],)) is None

    def test_yard_plant_requires_zone_and_name(self, user_client, db):
        resp = user_client.post("/yard/plants/new", data={"plant_name": "Lavender"})
        assert resp.status_code == 200
        assert db.one("SELECT 1 AS x FROM yard_plants") is None

    def test_edible_in_zone_shows_on_zone_page(self, user_client, db):
        entry = make_entry(user_client, db, "Garlic", zone_name="Veg Patch")
        assert b"Garlic" in user_client.get(f"/yard/zones/{entry['zone_id']}").data


class TestSettings:
    def test_location(self, user_client, db):
        user_client.post("/settings/location", data={"location": "Seattle, WA"})
        assert db.one("SELECT location FROM users WHERE id = ?", (user_client.user["id"],))["location"] == "Seattle, WA"
        user_client.post("/settings/location", data={"location": ""})
        assert db.one("SELECT location FROM users WHERE id = ?", (user_client.user["id"],))["location"] is None

    def test_photo_id_provider_rejects_unknown_values(self, user_client, db):
        user_client.post("/settings/photo-id-provider", data={"provider": "gemini"})
        assert db.one("SELECT photo_id_provider FROM users")["photo_id_provider"] == "gemini"
        user_client.post("/settings/photo-id-provider", data={"provider": "evil"})
        assert db.one("SELECT photo_id_provider FROM users")["photo_id_provider"] == "plantid"

    def test_fertilization_tracking_toggle(self, user_client, db):
        resp = user_client.post("/settings/fertilization-tracking", data={"enabled": "1"}, headers=AJAX)
        assert resp.get_json()["enabled"] in (1, True)
        assert user_client.get("/").status_code == 200


class TestSharedGarden:
    def _share(self, app, db):
        alice = app.test_client()
        signup(alice, "alice")
        bob = app.test_client()
        signup(bob, "bob")
        alice.post("/settings/share-garden", data={"partner_username": "bob"})
        assert db.one("SELECT confirmed FROM garden_shares")["confirmed"] == 0
        bob.post("/settings/share-garden", data={"partner_username": "Alice"})
        assert db.one("SELECT confirmed FROM garden_shares")["confirmed"] == 1
        return alice, bob

    def test_partner_sees_and_can_add_notes_to_shared_entries(self, app, db):
        alice, bob = self._share(app, db)
        entry = make_entry(alice, db, "Shared tomato")
        assert b"Shared tomato" in bob.get("/garden").data
        assert bob.get(f"/garden/{entry['id']}").status_code == 200
        resp = bob.post(f"/garden/{entry['id']}/photos", data={"photo_notes": "first ripe one"}, headers=AJAX)
        assert resp.get_json()["ok"] is True

    def test_pending_invite_does_not_share(self, app, db):
        alice = app.test_client()
        signup(alice, "alice")
        bob = app.test_client()
        signup(bob, "bob")
        alice.post("/settings/share-garden", data={"partner_username": "bob"})
        make_entry(alice, db, "Not yet shared")
        assert b"Not yet shared" not in bob.get("/garden").data

    def test_unshare_removes_access(self, app, db):
        alice, bob = self._share(app, db)
        make_entry(alice, db, "Formerly shared")
        share = db.one("SELECT id FROM garden_shares")
        bob.post(f"/settings/unshare-garden/{share['id']}")
        assert b"Formerly shared" not in bob.get("/garden").data

    def test_cannot_share_with_self_or_unknown_user(self, user_client, db):
        user_client.post("/settings/share-garden", data={"partner_username": "gardener"})
        user_client.post("/settings/share-garden", data={"partner_username": "nobody-here"})
        assert db.one("SELECT 1 AS x FROM garden_shares") is None


class TestChatWithoutApiKey:
    def test_chat_endpoints_return_json_when_unconfigured(self, user_client, db):
        entry = make_entry(user_client, db)
        for path, body in (("/api/entry-chat", {"message": "hi", "entry_id": entry["id"]}),
                           ("/api/garden-chat", {"message": "hi"})):
            resp = user_client.post(path, json=body)
            assert resp.status_code == 503 and resp.is_json, path
