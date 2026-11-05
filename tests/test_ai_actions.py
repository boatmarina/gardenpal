"""AI assistant actions, run against a scripted fake of the Anthropic client."""

from types import SimpleNamespace

import pytest

from tests.conftest import signup
from tests.test_garden import AJAX, make_entry


def tool_call(name, **inp):
    return SimpleNamespace(type="tool_use", id=f"tu_{name}", name=name, input=inp)


def reply(text, *blocks, stop="end_turn"):
    content = list(blocks) + ([SimpleNamespace(type="text", text=text)] if text else [])
    return SimpleNamespace(stop_reason=stop, content=content)


@pytest.fixture
def fake_ai(monkeypatch):
    """Replace Anthropic with a fake. Queue responses on .script; chat calls (those passing
    tools) consume them in order, anything else gets a harmless text reply."""
    import anthropic
    import gardenpal.web as web

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(web._API_LIMITS, "chat", {"daily": 1000, "burst_secs": 0})
    state = SimpleNamespace(script=[], calls=[])

    class _Messages:
        def create(self, **kw):
            if "tools" not in kw:
                return reply("ok")
            state.calls.append({**kw, "messages": list(kw["messages"])})  # the app keeps appending to its list
            return state.script.pop(0) if state.script else reply("Done.")

    class _Client:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    return state


def tool_results(call):
    """The tool_result contents the app sent back to the model in a follow-up call."""
    last = call["messages"][-1]["content"]
    return [b["content"] for b in last if isinstance(b, dict) and b.get("type") == "tool_result"]


def done_flag(db, entry_id):
    return db.one("SELECT done_for_season FROM garden_entries WHERE id = ?", (entry_id,))["done_for_season"]


class TestEntryChatDoneForSeason:
    def test_marks_this_plant_done_and_reports_it(self, user_client, db, fake_ai):
        entry = make_entry(user_client, db, "Peas")
        fake_ai.script += [
            reply("", tool_call("set_done_for_season", done=True), stop="tool_use"),
            reply("Marked your peas as done for the season."),
        ]
        resp = user_client.post("/api/entry-chat", json={"message": "peas are finished", "entry_id": entry["id"]})
        data = resp.get_json()
        assert resp.status_code == 200, data
        assert data["done_for_season"] == 1
        assert done_flag(db, entry["id"]) == 1
        assert db.one("SELECT 1 AS x FROM activity_log WHERE action = 'garden_done_season'")
        assert any(t["name"] == "set_done_for_season" for t in fake_ai.calls[0]["tools"])

    def test_can_undo(self, user_client, db, fake_ai):
        entry = make_entry(user_client, db, "Peas")
        user_client.post(f"/garden/{entry['id']}/toggle-done", headers=AJAX)
        fake_ai.script += [reply("", tool_call("set_done_for_season", done=False), stop="tool_use"), reply("Undone.")]
        data = user_client.post("/api/entry-chat", json={"message": "not done yet", "entry_id": entry["id"]}).get_json()
        assert data["done_for_season"] == 0
        assert done_flag(db, entry["id"]) == 0

    def test_context_tells_the_ai_the_current_state(self, user_client, db, fake_ai):
        entry = make_entry(user_client, db, "Peas")
        user_client.post(f"/garden/{entry['id']}/toggle-done", headers=AJAX)
        user_client.post("/api/entry-chat", json={"message": "status?", "entry_id": entry["id"]})
        assert "done for the season" in fake_ai.calls[0]["system"].lower()


class TestListChatsDoneForSeason:
    @pytest.mark.parametrize("path", ["/api/garden-chat", "/api/home-chat"])
    def test_bulk_marks_own_entries_and_skips_others(self, app, user_client, db, fake_ai, path):
        mine_a = make_entry(user_client, db, "Bush beans")
        mine_b = make_entry(user_client, db, "Zucchini")
        stranger = app.test_client()
        signup(stranger, "stranger")
        theirs = make_entry(stranger, db, "Stranger squash")

        fake_ai.script += [
            reply("", tool_call("set_done_for_season", entry_ids=[mine_a["id"], mine_b["id"], theirs["id"], 99999],
                                done=True), stop="tool_use"),
            reply("Done."),
        ]
        resp = user_client.post(path, json={"message": "beans and zucchini are done"})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["changed"] is True
        assert done_flag(db, mine_a["id"]) == 1 and done_flag(db, mine_b["id"]) == 1
        assert done_flag(db, theirs["id"]) == 0
        results = " ".join(tool_results(fake_ai.calls[1]))
        assert "not found" in results.lower()

    def test_partner_entries_are_read_only(self, app, db, fake_ai):
        alice = app.test_client()
        signup(alice, "alice")
        bob = app.test_client()
        signup(bob, "bob")
        alice.post("/settings/share-garden", data={"partner_username": "bob"})
        bob.post("/settings/share-garden", data={"partner_username": "alice"})
        alices = make_entry(alice, db, "Alice tomato")

        fake_ai.script += [reply("", tool_call("set_done_for_season", entry_ids=[alices["id"]], done=True),
                                 stop="tool_use"), reply("Done.")]
        bob.post("/api/garden-chat", json={"message": "mark alice's tomato done"})
        assert done_flag(db, alices["id"]) == 0
        assert "read-only" in " ".join(tool_results(fake_ai.calls[1]))

    @pytest.mark.parametrize("path", ["/api/garden-chat", "/api/home-chat"])
    def test_context_shows_done_plants(self, user_client, db, fake_ai, path):
        entry = make_entry(user_client, db, "Garlic")
        user_client.post(f"/garden/{entry['id']}/toggle-done", headers=AJAX)
        user_client.post(path, json={"message": "what's still growing?"})
        assert "[done for the season]" in fake_ai.calls[0]["system"]


def test_toggle_button_still_works_the_same(user_client, db):
    entry = make_entry(user_client, db)
    db.run("UPDATE garden_entries SET next_watering_date = '2026-12-01' WHERE id = ?", (entry["id"],))
    data = user_client.post(f"/garden/{entry['id']}/toggle-done", headers=AJAX).get_json()
    assert data == {"ok": True, "done_for_season": 1}
    row = db.one("SELECT never_water, never_fertilize, next_watering_date FROM garden_entries WHERE id = ?",
                 (entry["id"],))
    assert row == {"never_water": 1, "never_fertilize": 1, "next_watering_date": None}
