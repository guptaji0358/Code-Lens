"""LIAS knowledge store: SQLite + FTS5, provenance, contradiction handling, expiry, audit."""
import json
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone

from common import LiasError, SCHEMA_VERSION

CATEGORIES = {
    "project_fact", "architecture_decision", "preference", "build_config",
    "validated_command", "known_error", "automation", "temporary", "historical",
}

# Patterns that must never be persisted.
SECRET_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token|bearer)\b\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b\d{3}-\d{2}-\d{4}\b"),
]


QUERY_STOP = set("""the a an and or of to in on for with is are was were be this that it its as at by from
how what why where when which who do does did can could should would i we you me my our your please use
using file files code make add fix change update show tell about into not class""".split())


class SecretRejected(LiasError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def contains_secret(text):
    return any(p.search(text) for p in SECRET_PATTERNS)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS records(
  id TEXT PRIMARY KEY, key TEXT, category TEXT NOT NULL, content TEXT NOT NULL,
  source TEXT NOT NULL, created TEXT NOT NULL, verified_at TEXT,
  status TEXT NOT NULL,           -- verified | unverified | invalidated | superseded
  confidence TEXT NOT NULL,       -- high: user-confirmed/test-validated, medium: observed, low: inferred
  expires_at TEXT, project_id TEXT NOT NULL, related TEXT DEFAULT '[]', version INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_records_key ON records(key);
CREATE VIRTUAL TABLE IF NOT EXISTS kfts USING fts5(id UNINDEXED, content, category, key, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS audit(ts TEXT, action TEXT, record_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, automation_id TEXT, status TEXT,
  duration_ms INTEGER, cached INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS cache(
  ckey TEXT PRIMARY KEY, automation_id TEXT, created REAL, expires REAL, result TEXT);
CREATE TABLE IF NOT EXISTS automation_state(
  automation_id TEXT PRIMARY KEY, status TEXT, consecutive_failures INTEGER DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS observations(
  signature TEXT PRIMARY KEY, description TEXT, count INTEGER, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY, category TEXT, key TEXT, content TEXT, source TEXT, created TEXT,
  expires_at TEXT, reason TEXT, existing_id TEXT, state TEXT NOT NULL DEFAULT 'pending');
"""


def migrate(con, pid, root):
    """Create/upgrade schema; bind DB to a project identity (refuses foreign DBs)."""
    has_meta = con.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if has_meta else None
    if row is None:
        con.executescript(_SCHEMA)
        con.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", [
            ("schema_version", str(SCHEMA_VERSION)), ("project_id", pid), ("root", root)])
        con.commit()
        return
    bound = con.execute("SELECT value FROM meta WHERE key='project_id'").fetchone()[0]
    if bound != pid:
        raise LiasError(
            "database belongs to another project path (bound %s, current %s). "
            "Project was moved/copied: run `cli.py rebind` to adopt it deliberately." % (bound, pid))
    ver = int(row[0])
    if ver > SCHEMA_VERSION:
        raise LiasError("database schema %s is newer than this LIAS (%s)" % (ver, SCHEMA_VERSION))
    if ver < SCHEMA_VERSION:
        # v1 -> v2: adds `candidates`. Back up first; additive DDL + version bump in one transaction.
        dbfile = con.execute("PRAGMA database_list").fetchone()[2]
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if dbfile:
            shutil.copy2(dbfile, dbfile + ".pre-v%d-%d.bak" % (SCHEMA_VERSION, int(time.time())))
        try:
            con.execute("BEGIN IMMEDIATE")
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    con.execute(stmt)
            if ver < 3:  # v3: stemming tokenizer. Rebuild the index from the source-of-truth records table.
                con.execute("DROP TABLE kfts")
                con.execute("CREATE VIRTUAL TABLE kfts USING fts5(id UNINDEXED, content, category, key, "
                            "tokenize='porter unicode61')")
                con.execute("INSERT INTO kfts(id,content,category,key) SELECT id,content,category,COALESCE(key,'') "
                            "FROM records WHERE status IN ('verified','unverified')")
            con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
            con.commit()
        except Exception:
            con.rollback()
            raise
    else:
        con.executescript(_SCHEMA)


def rebind(lias):
    con = lias.db_path
    import sqlite3
    c = sqlite3.connect(str(con))
    shutil.copy2(con, str(con) + ".bak-%d" % int(time.time()))
    with c:
        c.execute("UPDATE meta SET value=? WHERE key='project_id'", (lias.pid,))
        c.execute("UPDATE meta SET value=? WHERE key='root'", (str(lias.root),))
        c.execute("UPDATE records SET project_id=?", (lias.pid,))
    c.close()


def _audit(con, action, rid, detail=""):
    con.execute("INSERT INTO audit VALUES(?,?,?,?)", (now_iso(), action, rid, detail))


class Knowledge:
    def __init__(self, lias):
        self.l = lias

    @property
    def con(self):
        return self.l.db()

    def add(self, category, content, source, key=None, verified=False,
            confidence=None, ttl_days=None, supersede=False, related=None):
        """Store a record. Returns {'result': created|refreshed|conflict, ...}."""
        if category not in CATEGORIES:
            raise LiasError("unknown category %r; valid: %s" % (category, sorted(CATEGORIES)))
        content = content.strip()
        if not content or not source:
            raise LiasError("content and source (provenance) are required")
        if contains_secret(content) or contains_secret(key or ""):
            raise SecretRejected("content looks like a credential/secret and was not stored")
        if category == "temporary" and ttl_days is None:
            ttl_days = 1
        if not verified and ttl_days is None:
            ttl_days = self.l.config["learning"]["unverified_ttl_days"]
        con = self.con
        if key:
            cur = con.execute(
                "SELECT * FROM records WHERE key=? AND project_id=? AND status IN ('verified','unverified')",
                (key, self.l.pid)).fetchall()
            for old in cur:
                if old["content"] == content:
                    if verified:
                        con.execute("UPDATE records SET verified_at=?, status='verified' WHERE id=?",
                                    (now_iso(), old["id"]))
                    _audit(con, "refresh", old["id"])
                    con.commit()
                    return {"result": "refreshed", "id": old["id"]}
            confirmed = [o for o in cur if o["status"] == "verified"]
            if confirmed and not supersede:
                _audit(con, "conflict", confirmed[0]["id"], "rejected: " + content[:120])
                con.commit()
                return {"result": "conflict", "existing_id": confirmed[0]["id"],
                        "existing": confirmed[0]["content"],
                        "hint": "confirm with supersede=True or invalidate the old record first"}
            for o in cur:
                if supersede or o["status"] == "unverified":
                    con.execute("UPDATE records SET status='superseded' WHERE id=?", (o["id"],))
                    con.execute("DELETE FROM kfts WHERE id=?", (o["id"],))
                    _audit(con, "supersede", o["id"])
        rid = uuid.uuid4().hex[:12]
        ts = now_iso()
        exp = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat(timespec="seconds") \
            if ttl_days else None
        conf = confidence or ("high" if verified else "low")
        con.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)", (
            rid, key, category, content, source, ts, ts if verified else None,
            "verified" if verified else "unverified", conf, exp, self.l.pid,
            json.dumps(related or [])))
        con.execute("INSERT INTO kfts(id,content,category,key) VALUES(?,?,?,?)",
                    (rid, content, category, key or ""))
        _audit(con, "create", rid, category)
        con.commit()
        return {"result": "created", "id": rid, "status": "verified" if verified else "unverified"}

    def invalidate(self, rid, reason=""):
        con = self.con
        n = con.execute("UPDATE records SET status='invalidated' WHERE id=? AND status!='invalidated'",
                        (rid,)).rowcount
        con.execute("DELETE FROM kfts WHERE id=?", (rid,))
        _audit(con, "invalidate", rid, reason)
        con.commit()
        return n == 1

    def verify(self, rid):
        """User/test confirmation of an existing record; clears the unverified expiry."""
        con = self.con
        n = con.execute("UPDATE records SET status='verified', verified_at=?, confidence='high', "
                        "expires_at=NULL WHERE id=? AND status='unverified'", (now_iso(), rid)).rowcount
        _audit(con, "verify", rid)
        con.commit()
        return n == 1

    def active_records(self):
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM records WHERE project_id=? AND status IN ('verified','unverified')", (self.l.pid,))
            if not self._expired(r)]

    def delete(self, rid):
        con = self.con
        n = con.execute("DELETE FROM records WHERE id=?", (rid,)).rowcount
        con.execute("DELETE FROM kfts WHERE id=?", (rid,))
        _audit(con, "delete", rid)
        con.commit()
        return n == 1

    @staticmethod
    def _expired(row):
        return bool(row["expires_at"]) and row["expires_at"] < now_iso()

    def search(self, query, limit=None, max_chars=None):
        cfg = self.l.config["retrieval"]
        limit = limit or cfg["max_results"]
        budget = max_chars or cfg["max_chars"]
        tokens = re.findall(r"[A-Za-z0-9_]{2,}", query.lower())
        tokens = [t for t in tokens if t not in QUERY_STOP] or tokens
        if not tokens:
            return []
        con = self.con
        seen, rows = set(), []

        def take(r, how):
            if r["id"] in seen or r["project_id"] != self.l.pid:
                return
            if r["status"] in ("invalidated", "superseded") or self._expired(r):
                return
            seen.add(r["id"])
            rows.append((r, how))

        qkey = query.strip()
        for r in con.execute("SELECT * FROM records WHERE key=?", (qkey,)):
            take(r, "exact")
        fts_q = " OR ".join('"%s"' % t for t in tokens)
        for r in con.execute(
                "SELECT r.* FROM kfts JOIN records r ON r.id=kfts.id "
                "WHERE kfts MATCH ? ORDER BY bm25(kfts) LIMIT ?", (fts_q, limit * 3)):
            take(r, "fts")
        out, used = [], 0
        for r, how in rows[:limit]:
            item = {"id": r["id"], "category": r["category"], "content": r["content"],
                    "status": r["status"], "confidence": r["confidence"], "source": r["source"],
                    "verified_at": r["verified_at"], "expires_at": r["expires_at"], "match": how}
            used += len(r["content"])
            if used > budget and out:
                break
            if used > budget:
                item["content"] = r["content"][:budget] + "…"
            out.append(item)
        return out

    def stats(self):
        c = self.con
        by = {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM records GROUP BY status")}
        return {"records": by, "expired": sum(1 for r in c.execute(
            "SELECT expires_at FROM records WHERE expires_at IS NOT NULL AND status IN ('verified','unverified')")
            if r[0] < now_iso())}

    def purge_expired(self):
        con = self.con
        ids = [r[0] for r in con.execute(
            "SELECT id FROM records WHERE expires_at IS NOT NULL AND expires_at < ?", (now_iso(),))]
        for i in ids:
            con.execute("UPDATE records SET status='invalidated' WHERE id=?", (i,))
            con.execute("DELETE FROM kfts WHERE id=?", (i,))
            _audit(con, "expire", i)
        con.commit()
        return len(ids)
