"""LIAS maintenance: backup / restore / health / repair. Never touches files outside .claude/lias."""
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from common import LiasError, validate_config
from knowledge import Knowledge

KEEP_BACKUPS = 5


def _bdir(l):
    d = l.dir / "backups"
    d.mkdir(exist_ok=True)
    return d


def backup(l):
    """Consistent online backup via SQLite's backup API; verified, then old ones pruned."""
    dst = _bdir(l) / ("lias-%s.sqlite3" % time.strftime("%Y%m%d-%H%M%S"))
    n = 1
    while dst.exists():
        dst = dst.with_name("lias-%s-%d.sqlite3" % (time.strftime("%Y%m%d-%H%M%S"), n)); n += 1
    out = sqlite3.connect(str(dst))
    try:
        l.db().backup(out)
        if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LiasError("backup failed integrity check")
    except BaseException:
        out.close()
        dst.unlink(missing_ok=True)
        raise
    out.close()
    files = sorted(_bdir(l).glob("lias-*.sqlite3"), key=lambda p: p.stat().st_mtime)
    for old in files[:-KEEP_BACKUPS]:
        old.unlink()
    return {"backup": str(dst.relative_to(l.root)), "kept": min(len(files), KEEP_BACKUPS)}


def list_backups(l):
    return sorted(_bdir(l).glob("lias-*.sqlite3"), key=lambda p: p.stat().st_mtime)


def restore(l, path=None, force=False):
    """Replace the live DB with a verified backup (the current DB is kept as pre-restore-*.sqlite3)."""
    src = Path(path) if path else (list_backups(l) or [None])[-1]
    if src is None or not Path(src).is_file():
        raise LiasError("no backup available to restore")
    chk = sqlite3.connect("file:%s?mode=ro" % Path(src).as_posix(), uri=True)
    try:
        if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LiasError("backup %s fails integrity check; not restoring" % src)
        row = chk.execute("SELECT value FROM meta WHERE key='project_id'").fetchone()
        if row and row[0] != l.pid and not force:
            raise LiasError("backup belongs to a different project; refusing (use force only if the project moved)")
    except sqlite3.DatabaseError as e:
        raise LiasError("backup %s is not a valid LIAS database: %s" % (src, e))
    finally:
        chk.close()
    l.close()
    if l.db_path.exists():
        shutil.copy2(l.db_path, _bdir(l) / ("pre-restore-%d.sqlite3" % int(time.time())))
    for suffix in ("-wal", "-shm"):
        Path(str(l.db_path) + suffix).unlink(missing_ok=True)
    tmp = l.db_path.with_suffix(".restore.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, l.db_path)  # atomic swap
    return {"restored_from": str(Path(src).name)}


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def health(l):
    """Deep diagnostics. Returns {'issues': [...], 'checks': {...}}; issues empty means healthy."""
    from automation import Automations
    from growth import static_validate
    issues, checks = [], {}
    try:
        con = l.db()
    except LiasError as e:
        return {"issues": [str(e)], "checks": {}}
    checks["db_integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
    if checks["db_integrity"] != "ok":
        issues.append("database integrity check failed: run recover")
    checks["schema_version"] = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]

    active = {r[0] for r in con.execute("SELECT id FROM records WHERE status IN ('verified','unverified')")}
    indexed = {r[0] for r in con.execute("SELECT id FROM kfts")}
    missing, orphan = sorted(active - indexed), sorted(indexed - active)
    checks["fts"] = {"missing": len(missing), "orphan": len(orphan)}
    if missing or orphan:
        issues.append("search index out of sync (%d missing, %d orphan): run doctor --fix" % (len(missing), len(orphan)))

    bad_cfg = validate_config(l.config)
    checks["config_problems"] = bad_cfg
    issues += ["config: " + b for b in bad_cfg]

    a = Automations(l)
    reg = a.load()
    quarantined, tampered = [], []
    for aid, m in reg.items():
        if m.get("status") == "invalid":
            issues.append("invalid manifest %s: %s" % (m["id"], m.get("error")))
            continue
        if a.effective_status(m) == "quarantined":
            quarantined.append(aid)
        if m.get("generated"):
            p = a._entry_path(m)
            if m.get("sha256") != _sha(p) or static_validate(p.read_text(encoding="utf-8"), m["risk"]):
                tampered.append(aid)
    checks["quarantined"], checks["tampered"] = quarantined, tampered
    if quarantined:
        issues.append("quarantined automations: %s (investigate, then `reinstate <id>`)" % quarantined)
    if tampered:
        issues.append("generated automations changed since activation: %s" % tampered)

    stale = [p for p in l.dir.rglob("*.tmp") if time.time() - p.stat().st_mtime > 3600]
    checks["stale_tmp_files"] = len(stale)
    if stale:
        issues.append("%d stale temp file(s) from an interrupted write: run doctor --fix" % len(stale))

    bk = list_backups(l)
    checks["backups"] = len(bk)
    checks["newest_backup_age_days"] = round((time.time() - bk[-1].stat().st_mtime) / 86400, 1) if bk else None
    if not bk:
        issues.append("no backup yet: run `cli.py backup`")
    return {"issues": issues, "checks": checks}


def fix(l):
    """Safe repairs only: rebuild search index, delete stale temp files. Nothing user-authored is changed."""
    con = l.db()
    con.execute("DELETE FROM kfts")
    con.execute("INSERT INTO kfts(id,content,category,key) SELECT id,content,category,COALESCE(key,'') "
                "FROM records WHERE status IN ('verified','unverified')")
    con.commit()
    removed = 0
    for p in l.dir.rglob("*.tmp"):
        if time.time() - p.stat().st_mtime > 3600:
            p.unlink()
            removed += 1
    return {"fts_rebuilt": True, "stale_tmp_removed": removed}
