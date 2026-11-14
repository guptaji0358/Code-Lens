"""Rebuild CodeLens binaries from the existing PyInstaller specs / NSIS script, then launch the app.

Reuses the existing build definitions (specs/*.spec, CodeLensSetup.nsi) - nothing is redefined here.

Layout produced (matches the paths the specs and the .nsi already expect):
  out/CodeLens/, out/CodeLensCLI/, out/CodeLens_Uninstall.exe   <- the three app builds (Setup payload)
  dist/CodeLens Setup/                                          <- installer wizard (onedir)
  dist/CodeLens_Setup.exe                                       <- NSIS bootstrap (only if makensis is found)

Self-skipping: a fingerprint of the sources/specs is stored in build/.fingerprint; when nothing
changed and the outputs exist, the build is skipped (use --force to rebuild anyway).

Usage: python scripts/build_release.py [--force] [--no-launch] [--no-installer]
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
FP_FILE = os.path.join(BUILD, ".fingerprint")
NSI = os.path.join(ROOT, "CodeLensSetup.nsi")
APP_EXE = os.path.join(ROOT, "out", "CodeLens", "CodeLens.exe")
SETUP_EXE = os.path.join(ROOT, "dist", "CodeLens_Setup.exe")

# (spec, distpath) - order matters: the Setup spec bundles the three out/ builds as its payload.
STEPS = [
    ("CodeLens.spec", "out"),
    ("CodeLensCLI.spec", "out"),
    ("CodeLens_Uninstall.spec", "out"),
    ("CodeLens Setup.spec", "dist"),
]
FP_DIRS = ["scripts", "specs", "Assets", "Installer"]
FP_FILES = ["CodeLensSetup.nsi"]


def fingerprint():
    h = hashlib.sha256()
    paths = [os.path.join(ROOT, f) for f in FP_FILES]
    for d in FP_DIRS:
        for dp, dn, fn in os.walk(os.path.join(ROOT, d)):
            dn[:] = sorted(x for x in dn if x != "__pycache__")
            paths += [os.path.join(dp, f) for f in sorted(fn) if not f.endswith(".pyc")]
    for p in paths:
        if os.path.isfile(p):
            h.update(os.path.relpath(p, ROOT).encode())
            with open(p, "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


def outputs_exist(installer):
    need = [APP_EXE, os.path.join(ROOT, "out", "CodeLensCLI", "CodeLensCLI.exe"),
            os.path.join(ROOT, "out", "CodeLens_Uninstall.exe"),
            os.path.join(ROOT, "dist", "CodeLens Setup", "CodeLens Setup.exe")]
    return all(os.path.isfile(p) for p in need)


def find_makensis():
    exe = shutil.which("makensis")
    if exe:
        return exe
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            p = os.path.join(base, "NSIS", "makensis.exe")
            if os.path.isfile(p):
                return p
    return None


def run(argv, label):
    print("\n==> %s\n    %s" % (label, " ".join(argv)), flush=True)
    t = time.time()
    rc = subprocess.call(argv, cwd=ROOT)
    if rc != 0:
        print("!! %s failed (exit %d)" % (label, rc), flush=True)
        sys.exit(rc)
    print("    ok (%.0fs)" % (time.time() - t), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true", help="rebuild even if sources are unchanged")
    ap.add_argument("--no-launch", action="store_true", help="do not start the app afterwards")
    ap.add_argument("--no-installer", action="store_true", help="skip the Setup wizard + NSIS bootstrap")
    a = ap.parse_args()

    fp = fingerprint()
    old = ""
    if os.path.isfile(FP_FILE):
        with open(FP_FILE) as fh:
            old = fh.read().strip()
    up_to_date = (not a.force) and fp == old and outputs_exist(not a.no_installer)

    if up_to_date:
        print("Sources unchanged and binaries present - skipping build (use --force to rebuild).")
    else:
        steps = STEPS[:3] if a.no_installer else STEPS
        for spec, dist in steps:
            run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                 "--distpath", dist, "--workpath", os.path.join("build", os.path.splitext(spec)[0].replace(" ", "_")),
                 os.path.join("specs", spec)], "PyInstaller: " + spec)
        if not a.no_installer:
            mk = find_makensis()
            if mk:
                run([mk, NSI], "NSIS bootstrap")
            else:
                print("-- makensis not found; skipped dist\\CodeLens_Setup.exe")
        os.makedirs(BUILD, exist_ok=True)
        with open(FP_FILE, "w") as fh:
            fh.write(fp)

    print("\nBinaries:\n  app:       %s\n  installer: %s" % (APP_EXE, SETUP_EXE if os.path.isfile(SETUP_EXE) else "(not built)"))
    if not a.no_launch and os.path.isfile(APP_EXE):
        print("Launching CodeLens...")
        subprocess.Popen([APP_EXE], cwd=os.path.dirname(APP_EXE),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
