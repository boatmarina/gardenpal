"""
Shared fixtures for route-level tests.

Tests run the real Flask app against a throwaway local Postgres database.
They never touch Supabase: the URL must point at localhost or the run aborts.

Setup (once per machine / container):
    scripts/test_db_setup.sh

Run:
    pytest
"""

import os
import re
from urllib.parse import urlparse

import pg8000
import pytest
import requests

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/gardenpal_test"
)

# Anything that would reach a paid or external service is removed for the test run.
_EXTERNAL_ENV = (
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OCR_SPACE_API_KEY", "PERENUAL_API_KEY",
    "PLANT_ID_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_STORAGE_BUCKET", "VERCEL",
)


def _pg_connect(url, database=None):
    p = urlparse(url)
    return pg8000.connect(
        host=p.hostname, port=p.port or 5432, user=p.username,
        password=p.password, database=database or p.path.lstrip("/"),
    )


def _reset_database(url):
    p = urlparse(url)
    if p.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError(f"Refusing to run tests against non-local database host {p.hostname!r}")
    dbname = p.path.lstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]+", dbname):
        raise RuntimeError(f"Unexpected test database name {dbname!r}")

    admin = _pg_connect(url, database="postgres")
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    if cur.fetchone() is None:
        cur.execute(f"CREATE DATABASE {dbname}")
    admin.close()

    conn = _pg_connect(url)
    conn.autocommit = True
    conn.cursor().execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.close()


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    _reset_database(TEST_DATABASE_URL)

    saved = {k: os.environ.pop(k) for k in _EXTERNAL_ENV if k in os.environ}
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["SECRET_KEY"] = "test-secret"

    from gardenpal.web import create_app

    application = create_app()
    application.config.update(
        TESTING=True,
        SESSION_COOKIE_SECURE=False,
        UPLOAD_FOLDER=str(tmp_path_factory.mktemp("uploads")),
    )
    with application.test_client() as c:
        c.get("/auth/login")  # first request builds the schema
    yield application
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _clean_tables(app):
    conn = _pg_connect(TEST_DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    tables = [r[0] for r in cur.fetchall()]
    if tables:
        cur.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
    conn.close()
    yield


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """Fail fast instead of calling real weather, plant, or OCR APIs."""
    def _blocked(*args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")
    monkeypatch.setattr(requests.sessions.Session, "request", _blocked)


@pytest.fixture
def db():
    """Direct connection for asserting on stored data. Rows come back as dicts."""
    conn = _pg_connect(TEST_DATABASE_URL)
    conn.autocommit = True

    class _DB:
        def all(self, sql, params=()):
            cur = conn.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

        def one(self, sql, params=()):
            rows = self.all(sql, params)
            return rows[0] if rows else None

        def run(self, sql, params=()):
            conn.cursor().execute(sql.replace("?", "%s"), params)

    yield _DB()
    conn.close()


@pytest.fixture
def client(app):
    return app.test_client()


def signup(client, username="gardener", password="password123"):
    return client.post(
        "/auth/signup",
        data={"username": username, "password": password, "confirm_password": password},
    )


@pytest.fixture
def user_client(app, db):
    """A test client logged in as a freshly signed-up user."""
    c = app.test_client()
    resp = signup(c)
    assert resp.status_code == 302, resp.data[:500]
    c.user = db.one("SELECT * FROM users WHERE username = ?", ("gardener",))
    return c
