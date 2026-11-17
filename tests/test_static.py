import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent / "static"
bad = 0

for name in ["index", "sender", "reciever"]:
    src = (ROOT / f"{name}.html").read_text(encoding="utf-8")
    present = set(re.findall(r'\bid="([^"]+)"', src))
    used = set(re.findall(r"""\$\(['"]([^'"]+)['"]\)""", src))
    used |= set(re.findall(r"""getElementById\(['"]([^'"]+)['"]\)""", src))
    missing = sorted(used - present)
    unused = sorted(present - used)
    print(f"\n{name}.html: {len(present)} ids declared, {len(used)} referenced")
    if missing:
        bad += len(missing)
        print(f"  MISSING (referenced but not in markup): {missing}")
    else:
        print("  OK  every referenced id exists")
    if unused:
        print(f"  note: declared but not referenced in JS: {unused}")

print("\n--- import paths ---")
for name in ["index", "sender", "reciever"]:
    src = (ROOT / f"{name}.html").read_text(encoding="utf-8")
    for spec in re.findall(r"""from\s+['"](/[^'"]+)['"]""", src):
        p = ROOT / spec.lstrip("/")
        ok = p.is_file()
        if not ok:
            bad += 1
        print(f"  {'OK  ' if ok else 'MISS'} {name}.html -> {spec}")

print("\n--- local asset refs ---")
for name in ["index", "sender", "reciever"]:
    src = (ROOT / f"{name}.html").read_text(encoding="utf-8")
    for spec in re.findall(r"""(?:href|src)="(/[^"]+)\"""", src):
        p = ROOT / spec.lstrip("/")
        ok = p.is_file()
        if not ok:
            bad += 1
        print(f"  {'OK  ' if ok else 'MISS'} {name}.html -> {spec}")

# --- .env must load before any project module ---------------------------
# storage, backends and uploads read their configuration at import time,
# so a load_dotenv() placed even one import too low silently ignores .env.
_server = (ROOT.parent / "server.py").read_text(encoding="utf-8").splitlines()
_load_at = next((i for i, l in enumerate(_server)
                 if l.startswith("load_dotenv(")), None)
_project = ('import storage', 'import uploads', 'import backends',
            'import index_db', 'import compression')
_first_project = next((i for i, l in enumerate(_server)
                       if l.startswith(_project)), None)
_ok = (_load_at is not None and _first_project is not None
       and _load_at < _first_project)
if not _ok:
    bad += 1
print(f"  {'OK  ' if _ok else 'MISS'} server.py loads .env before any project import (load_dotenv at {_load_at}, first import at {_first_project})")

print(f"\n{'=' * 46}\n  {'FAILURES: ' + str(bad) if bad else 'all id and path checks passed'}\n{'=' * 46}")
sys.exit(1 if bad else 0)
