"""SQLite storage: id mapping, encrypted secrets, response cache and closed energy periods."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from typing import Any

from .crypto import new_opaque_id

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS secrets (
  name TEXT PRIMARY KEY,
  value BLOB NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  name TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS homes (
  id TEXT PRIMARY KEY,
  gql_id TEXT UNIQUE,
  data_id TEXT UNIQUE,
  name TEXT NOT NULL,
  time_zone TEXT NOT NULL,
  realtime INTEGER NOT NULL DEFAULT 0,
  seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  home_id TEXT NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
  data_id TEXT NOT NULL,
  UNIQUE (home_id, data_id)
);
CREATE TABLE IF NOT EXISTS cursors (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  etag TEXT,
  fetched_at REAL NOT NULL,
  stale_since REAL
);
CREATE TABLE IF NOT EXISTS energy (
  home_id TEXT NOT NULL,
  type TEXT NOT NULL,
  resolution TEXT NOT NULL,
  from_ts TEXT NOT NULL,
  to_ts TEXT NOT NULL,
  energy REAL,
  cost REAL,
  unit_price REAL,
  currency TEXT,
  PRIMARY KEY (home_id, type, resolution, from_ts)
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_pending (
  state TEXT PRIMARY KEY,
  verifier BLOB NOT NULL,
  scopes TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""

CURSOR_TTL_S = 24 * 3600


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _x(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))

    def ping(self) -> bool:
        return self._q("SELECT 1")[0][0] == 1

    # --- secrets (values are already encrypted by the caller) ---
    def put_secret(self, name: str, value: bytes) -> None:
        self._x(
            "INSERT INTO secrets(name, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (name, value, time.time()),
        )

    def get_secret(self, name: str) -> bytes | None:
        rows = self._q("SELECT value FROM secrets WHERE name=?", (name,))
        return rows[0]["value"] if rows else None

    def delete_secret(self, name: str) -> None:
        self._x("DELETE FROM secrets WHERE name=?", (name,))

    # --- small non-secret values ---
    def put_kv(self, name: str, value: Any) -> None:
        self._x(
            "INSERT INTO kv(name, value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (name, json.dumps(value)),
        )

    def get_kv(self, name: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM kv WHERE name=?", (name,))
        return json.loads(rows[0]["value"]) if rows else default

    def delete_kv(self, name: str) -> None:
        self._x("DELETE FROM kv WHERE name=?", (name,))

    # --- homes ---
    def upsert_home(self, *, gql_id: str | None, data_id: str | None, name: str, time_zone: str, realtime: bool) -> str:
        """Find the home by either upstream id, merge the other id in, and return its opaque id."""
        with self._lock:
            row = None
            for col, val in (("gql_id", gql_id), ("data_id", data_id)):
                if val and row is None:
                    row = self._conn.execute(f"SELECT * FROM homes WHERE {col}=?", (val,)).fetchone()  # noqa: S608 - col is a literal
            now = time.time()
            if row is None:
                hid = new_opaque_id()
                self._conn.execute(
                    "INSERT INTO homes(id,gql_id,data_id,name,time_zone,realtime,seen_at) VALUES (?,?,?,?,?,?,?)",
                    (hid, gql_id, data_id, name, time_zone, int(realtime), now),
                )
                return hid
            self._conn.execute(
                "UPDATE homes SET gql_id=COALESCE(?,gql_id), data_id=COALESCE(?,data_id), name=?, "
                "time_zone=?, realtime=?, seen_at=? WHERE id=?",
                (gql_id, data_id, name, time_zone, int(realtime), now, row["id"]),
            )
            return row["id"]

    def get_home(self, home_id: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM homes WHERE id=?", (home_id,))
        return rows[0] if rows else None

    def list_homes(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM homes ORDER BY name")

    def delete_homes_not_in(self, keep: set[str]) -> None:
        for row in self.list_homes():
            if row["id"] not in keep:
                self._x("DELETE FROM homes WHERE id=?", (row["id"],))

    # --- devices ---
    def device_id_for(self, home_id: str, data_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM devices WHERE home_id=? AND data_id=?", (home_id, data_id)
            ).fetchone()
            if row:
                return row["id"]
            did = new_opaque_id()
            self._conn.execute("INSERT INTO devices(id,home_id,data_id) VALUES (?,?,?)", (did, home_id, data_id))
            return did

    def get_device(self, home_id: str, device_id: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM devices WHERE id=? AND home_id=?", (device_id, home_id))
        return rows[0] if rows else None

    # --- history cursors ---
    def put_cursor(self, device_id: str, url: str) -> str:
        cid = new_opaque_id()
        now = time.time()
        self._x("DELETE FROM cursors WHERE created_at < ?", (now - CURSOR_TTL_S,))
        self._x("INSERT INTO cursors(id,device_id,url,created_at) VALUES (?,?,?,?)", (cid, device_id, url, now))
        return cid

    def get_cursor(self, device_id: str, cursor_id: str) -> str | None:
        rows = self._q(
            "SELECT url FROM cursors WHERE id=? AND device_id=? AND created_at >= ?",
            (cursor_id, device_id, time.time() - CURSOR_TTL_S),
        )
        return rows[0]["url"] if rows else None

    # --- cache ---
    def cache_get(self, key: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM cache WHERE key=?", (key,))
        return rows[0] if rows else None

    def cache_put(self, key: str, body: Any, etag: str | None = None) -> None:
        self._x(
            "INSERT INTO cache(key,body,etag,fetched_at,stale_since) VALUES (?,?,?,?,NULL) "
            "ON CONFLICT(key) DO UPDATE SET body=excluded.body, etag=excluded.etag, "
            "fetched_at=excluded.fetched_at, stale_since=NULL",
            (key, json.dumps(body), etag, time.time()),
        )

    def cache_touch(self, key: str) -> None:
        self._x("UPDATE cache SET fetched_at=?, stale_since=NULL WHERE key=?", (time.time(), key))

    def cache_mark_stale(self, key: str) -> None:
        self._x("UPDATE cache SET stale_since=COALESCE(stale_since, ?) WHERE key=?", (time.time(), key))

    def cache_clear(self, prefix: str = "") -> None:
        self._x("DELETE FROM cache WHERE key LIKE ?", (prefix + "%",))

    # --- closed energy periods (kept for good) ---
    def put_energy(self, home_id: str, type_: str, resolution: str, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO energy(home_id,type,resolution,from_ts,to_ts,energy,cost,unit_price,currency) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        home_id,
                        type_,
                        resolution,
                        r["from"],
                        r["to"],
                        r["energy"],
                        r["cost"],
                        r["unitPrice"],
                        r["currency"],
                    )
                    for r in rows
                ],
            )

    def get_energy(self, home_id: str, type_: str, resolution: str) -> list[dict[str, Any]]:
        rows = self._q(
            "SELECT from_ts,to_ts,energy,cost,unit_price,currency FROM energy "
            "WHERE home_id=? AND type=? AND resolution=? ORDER BY from_ts",
            (home_id, type_, resolution),
        )
        return [
            {"from": r[0], "to": r[1], "energy": r[2], "cost": r[3], "unitPrice": r[4], "currency": r[5]} for r in rows
        ]

    # --- sessions ---
    def put_session(self, token_hash: str, expires_at: float) -> None:
        self._x("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        self._x("INSERT INTO sessions(token_hash, expires_at) VALUES (?,?)", (token_hash, expires_at))

    def session_valid(self, token_hash: str) -> bool:
        return bool(self._q("SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash, time.time())))

    def delete_session(self, token_hash: str) -> None:
        self._x("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    # --- pending OAuth flows ---
    def put_oauth_pending(self, state: str, verifier: bytes, scopes: list[str]) -> None:
        now = time.time()
        self._x("DELETE FROM oauth_pending WHERE created_at < ?", (now - 600,))
        self._x(
            "INSERT INTO oauth_pending(state,verifier,scopes,created_at) VALUES (?,?,?,?)",
            (state, verifier, json.dumps(scopes), now),
        )

    def pop_oauth_pending(self, state: str) -> tuple[bytes, list[str]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT verifier, scopes FROM oauth_pending WHERE state=? AND created_at>=?",
                (state, time.time() - 600),
            ).fetchone()
            self._conn.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
        return (row["verifier"], json.loads(row["scopes"])) if row else None
