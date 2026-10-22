import pytest

from tests.conftest import signup


class TestAuth:
    def test_signup_logs_in_and_redirects_home(self, client, db):
        resp = signup(client, "newuser")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")
        assert db.one("SELECT username FROM users WHERE username = 'newuser'")
        assert client.get("/").status_code == 200

    def test_signup_rejects_short_password(self, client, db):
        resp = client.post("/auth/signup", data={"username": "abc", "password": "short", "confirm_password": "short"})
        assert resp.status_code == 200
        assert b"at least 8 characters" in resp.data
        assert db.one("SELECT 1 AS x FROM users") is None

    def test_signup_rejects_duplicate_username_case_insensitive(self, client):
        signup(client, "Rosie")
        client.post("/auth/logout")
        resp = signup(client, "rosie")
        assert b"already taken" in resp.data

    def test_login_and_logout(self, client):
        signup(client, "pat")
        client.post("/auth/logout")
        assert client.get("/").status_code == 302

        bad = client.post("/auth/login", data={"username": "pat", "password": "wrongpass1"})
        assert b"Invalid username or password" in bad.data

        ok = client.post("/auth/login", data={"username": "PAT", "password": "password123"})
        assert ok.status_code == 302
        assert client.get("/").status_code == 200

    def test_session_is_persistent(self, client):
        resp = signup(client, "stay")
        cookie = resp.headers.get("Set-Cookie", "")
        assert "Expires=" in cookie, "session cookie should persist across app restarts"

    def test_protected_pages_redirect_when_logged_out(self, client):
        for path in ("/", "/garden", "/ideas", "/yard", "/tools", "/garden/new"):
            resp = client.get(path)
            assert resp.status_code == 302, path
            assert "/auth/login" in resp.headers["Location"], path


MAIN_PAGES = [
    "/", "/garden", "/garden/new", "/ideas", "/ideas/new", "/yard", "/yard/zones/new",
    "/yard/plants/new", "/tools", "/export/diary",
    "/export/library.csv", "/export/yard.csv", "/export/garden.csv",
]


@pytest.mark.parametrize("path", MAIN_PAGES)
def test_main_pages_render_for_new_user(user_client, path):
    resp = user_client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}\n{resp.data[:800]!r}"


def test_unknown_page_shows_404(user_client):
    resp = user_client.get("/does-not-exist")
    assert resp.status_code == 404
