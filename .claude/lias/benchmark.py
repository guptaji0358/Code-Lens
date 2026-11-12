"""Reproducible LIAS benchmarks. Measures local time and context-size PROXIES only.

Model tokens cannot be measured from inside a local script, so no token savings are claimed here.
Proxy used: chars/4 ~ tokens (rough, English/code average) for "what Claude would have to read" vs
"what LIAS returns". Treat proxies as indicative, not as billing numbers.
"""
import json
import os
import shutil
import statistics
import tempfile
import time
from pathlib import Path

from common import Lias, LIAS_VERSION
from knowledge import Knowledge
from automation import Automations

SEED = [  # (key, fact, query that should retrieve it)
    ("engine", "All file loading and search logic lives in CodeLensCLI.py and the GUI imports it as core", "where does the search engine live"),
    ("entry", "CodeLens.py is a thin entry point that sets DPI awareness before creating the QApplication", "why is DPI awareness set in the entry point"),
    ("theme", "theme.py owns colors stylesheet icon helpers and BASE_DIR", "which file defines icons and stylesheet colors"),
    ("persist", "shortcuts.json and recent_folders.json are saved next to the running exe", "where are keyboard shortcuts persisted"),
    ("async", "slow work runs through _run_busy using an _AsyncWorker on a QThread", "how do background tasks run in the GUI"),
    ("signals", "worker finished failed signals must connect to bound QObject methods never lambdas", "can I use a lambda for worker signals"),
    ("watch", "QFileSystemWatcher debounced into _do_live_rescan which calls rescan_sources", "how does live folder rescan work"),
    ("ctx", "Explorer context menu entries are registered under HKCU Software Classes via a VBScript launcher", "how is the explorer context menu registered"),
    ("dialogs", "AnimatedDialog replaces QMessageBox for every popup in the application", "which class shows info and error popups"),
    ("crash", "crash tracebacks are written to LOCALAPPDATA CodeLens_crash.log", "where is the crash log written"),
]


def _timeit(fn, n):
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    return {"n": n, "median_ms": round(statistics.median(ts), 2), "min_ms": round(min(ts), 2), "max_ms": round(max(ts), 2)}


def bench_retrieval():
    root = Path(tempfile.mkdtemp(prefix="lias_bench_"))
    try:
        (root / ".claude" / "lias").mkdir(parents=True)
        l = Lias(root)
        k = Knowledge(l)
        for key, fact, _ in SEED:
            k.add("project_fact", fact, "benchmark-fixture", key=key, verified=True)
        hits = 0
        for _, fact, q in SEED:
            r = k.search(q, limit=3)
            hits += bool(r and r[0]["content"] == fact)
        lat = _timeit(lambda: k.search("where does the search engine live"), 50)
        l.close()
        return {"records": len(SEED), "top1_accuracy": round(hits / len(SEED), 2), "latency": lat}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def bench_automation(l, n=5):
    a = Automations(l)
    if "source-stats" not in a.load():
        return {"skipped": "source-stats not registered"}
    inp = {"extensions": [".py"]}
    cold = _timeit(lambda: a.run("source-stats", inp, use_cache=False), n)
    a.run("source-stats", inp)  # populate cache
    warm = _timeit(lambda: a.run("source-stats", inp), n * 4)
    env = a.run("source-stats", inp)
    ret_chars = len(json.dumps(env["result"]))
    raw_bytes = 0
    for dp, dn, fn in os.walk(str(l.root)):
        dn[:] = [d for d in dn if d not in {".git", "__pycache__", "dist", "build", ".claude", "out"}]
        raw_bytes += sum(os.path.getsize(os.path.join(dp, f)) for f in fn if f.endswith(".py"))
    return {"cold_uncached": cold, "warm_cached": warm, "cache_hit_on_warm": env["cached"],
            "proxy_context": {"lias_result_chars": ret_chars, "source_bytes_to_read_manually": raw_bytes,
                              "approx_tokens_lias": ret_chars // 4, "approx_tokens_manual_read": raw_bytes // 4,
                              "caveat": "proxy: a manual count would not necessarily read every file in full"},
            "cache": a.cache_stats()}


def run(root):
    l = Lias(root)
    try:
        res = {"lias_version": LIAS_VERSION, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "retrieval": bench_retrieval(), "automation": bench_automation(l),
               "model_tokens": "NOT MEASURED (not observable from a local script)"}
        out = l.dir / "benchmarks"
        out.mkdir(exist_ok=True)
        (out / ("bench-%s.json" % time.strftime("%Y%m%d-%H%M%S"))).write_text(json.dumps(res, indent=2), encoding="utf-8")
        return res
    finally:
        l.close()
