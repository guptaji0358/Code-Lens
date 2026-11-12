"""LIAS hybrid learning: deterministic candidate detection, dedup, contradiction staging, command validation.

Policy (what is auto-saved as *verified*):
  - an explicit user instruction ("remember that ...") that contains no secret and no untrusted/pasted text
  - a command that the caller reports as executed successfully (exit code 0) and matches the allowlist
Everything else is staged as a candidate (pending, expires) or stored unverified (expires).
Conflicts with confirmed facts are NEVER overwritten: they are staged for the user to decide.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

from common import LiasError
from knowledge import Knowledge, SecretRejected, contains_secret, now_iso

PASTE = re.compile(r"(?is)<pasted_content\b[^>]*>.*?</pasted_content[^>]*>")
EXPLICIT = re.compile(
    r"(?im)^\s*(?:please\s+)?(?:remember|note|keep in mind|fyi)(?:\s+that)?\s*[:,\-]?\s+(?P<fact>\S.{7,400})$")
SUBJECT = re.compile(
    r"^(?P<s>[\w .\-/]{2,60}?)\s+(?:is|are|uses|use|requires|must be|equals|=)\s+(?P<v>\S.*)$", re.I)
TEMP = re.compile(r"(?i)\b(for now|today|this session|temporar|right now|currently debugging)\b")
CAT_RULES = [
    ("architecture_decision", re.compile(r"(?i)\b(decid|chose|architecture|design|we (?:use|chose))")),
    ("build_config", re.compile(r"(?i)\b(build|compile|pyinstaller|install|toolchain|version)")),
    ("preference", re.compile(r"(?i)\b(prefer|always|never|style|convention|don'?t)\b")),
    ("known_error", re.compile(r"(?i)\b(error|exception|fails?|crash|bug)\b")),
]


def _tokens(t):
    return set(re.findall(r"[a-z0-9_]+", t.lower()))


def jaccard(a, b):
    a, b = _tokens(a), _tokens(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def overlap(a, b, min_tokens=4):
    """Containment similarity: catches a short description contained in a longer one."""
    a, b = _tokens(a), _tokens(b)
    small = min(len(a), len(b))
    return len(a & b) / small if small >= min_tokens else 0.0


def classify(text):
    if TEMP.search(text):
        return "temporary"
    for cat, rx in CAT_RULES:
        if rx.search(text):
            return cat
    return "project_fact"


def subject_key(text):
    m = SUBJECT.match(text.strip().rstrip("."))
    if not m:
        return None
    s = re.sub(r"[^a-z0-9]+", "-", m.group("s").lower()).strip("-")
    return "subj:" + s if len(s) >= 2 else None


def extract_explicit(prompt):
    """Facts the user explicitly asked to remember. Pasted/untrusted blocks are ignored."""
    clean = PASTE.sub(" ", prompt or "")
    return [m.group("fact").strip() for m in EXPLICIT.finditer(clean)]


class Learner:
    def __init__(self, lias):
        self.l = lias
        self.k = Knowledge(lias)
        self.cfg = lias.config["learning"]

    # ---- candidates ----
    def stage(self, category, content, source, key, reason, existing_id=None):
        cid = uuid.uuid4().hex[:12]
        exp = (datetime.now(timezone.utc) + timedelta(days=self.cfg["candidate_ttl_days"])).isoformat(timespec="seconds")
        self.k.con.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
                           (cid, category, key, content, source, now_iso(), exp, reason, existing_id))
        self.k.con.commit()
        return cid

    def candidates(self, state="pending"):
        self.k.con.execute("UPDATE candidates SET state='expired' WHERE state='pending' AND expires_at<?", (now_iso(),))
        self.k.con.commit()
        return [dict(r) for r in self.k.con.execute(
            "SELECT * FROM candidates WHERE state=? ORDER BY created", (state,))]

    def promote(self, cid, supersede=False):
        r = self.k.con.execute("SELECT * FROM candidates WHERE id=? AND state='pending'", (cid,)).fetchone()
        if not r:
            raise LiasError("no pending candidate %s" % cid)
        res = self.k.add(r["category"], r["content"], r["source"] + " (user-promoted)", key=r["key"],
                         verified=True, supersede=supersede)
        if res["result"] != "conflict":
            self.k.con.execute("UPDATE candidates SET state='promoted' WHERE id=?", (cid,))
            self.k.con.commit()
        return res

    def reject(self, cid):
        n = self.k.con.execute("UPDATE candidates SET state='rejected' WHERE id=? AND state='pending'", (cid,)).rowcount
        self.k.con.commit()
        return n == 1

    # ---- learning ----
    def learn_fact(self, content, source, explicit=False, category=None, key=None):
        """Returns {'result': saved|refreshed|duplicate|staged|rejected_secret, ...}. Never raises on policy."""
        content = content.strip()
        if contains_secret(content):
            return {"result": "rejected_secret", "detail": "looks like a credential; not stored or staged"}
        category = category or classify(content)
        key = key or subject_key(content)
        # near-duplicate of an active record -> refresh, don't duplicate
        for r in self.k.active_records():
            if key and r["key"] == key and r["content"] != content:
                continue  # same subject, different claim: a contradiction, never a duplicate
            if r["content"] == content or jaccard(r["content"], content) >= self.cfg["near_duplicate_jaccard"]:
                if explicit and r["status"] == "unverified":
                    self.k.verify(r["id"])
                    return {"result": "refreshed", "id": r["id"], "detail": "verified existing record"}
                return {"result": "duplicate", "id": r["id"]}
        try:
            res = self.k.add(category, content, source, key=key, verified=explicit)
        except SecretRejected:
            return {"result": "rejected_secret"}
        if res["result"] == "conflict":
            cid = self.stage(category, content, source, key,
                             "contradicts verified record %s" % res["existing_id"], res["existing_id"])
            return {"result": "staged", "candidate_id": cid, "existing": res["existing"],
                    "existing_id": res["existing_id"], "reason": "conflicts with a confirmed fact"}
        return {"result": "saved" if res["result"] == "created" else res["result"], "id": res["id"],
                "status": res.get("status", "verified"), "category": category}

    def learn_from_prompt(self, prompt, source="user-prompt"):
        return [dict(self.learn_fact(f, source, explicit=True), fact=f[:120]) for f in extract_explicit(prompt)]

    def learn_command(self, command, exit_code, evidence="executed, exit code 0"):
        command = " ".join(command.split())
        if exit_code != 0:
            return {"result": "ignored", "detail": "only successful (exit 0) commands are learned"}
        if len(command) > 300 or contains_secret(command):
            return {"result": "rejected_secret" if contains_secret(command) else "ignored"}
        if not any(command.startswith(p) for p in self.cfg["command_prefixes"]):
            return {"result": "ignored", "detail": "command not on the learning allowlist"}
        res = self.k.add("validated_command", command, evidence, key="cmd:" + command[:80],
                         verified=True, confidence="medium")
        return {"result": res["result"], "id": res.get("id") or res.get("existing_id")}
