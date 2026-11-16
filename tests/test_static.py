"""Static check: every $('id') / getElementById('id') used in a page exists in it."""
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
    # ids used only by CSS/aria are fine; list them for eyeballing only
    if unused:
        print(f"  note: declared but not referenced in JS: {unused}")

# also confirm every import path resolves on disk
print("\n--- import paths ---")
for name in ["index", "sender", "reciever"]:
    src = (ROOT / f"{name}.html").read_text(encoding="utf-8")
    for spec in re.findall(r"""from\s+['"](/[^'"]+)['"]""", src):
        p = ROOT / spec.lstrip("/")
        ok = p.is_file()
        if not ok:
            bad += 1
        print(f"  {'OK  ' if ok else 'MISS'} {name}.html -> {spec}")

# and that stylesheet/script srcs exist locally where they are local
print("\n--- local asset refs ---")
for name in ["index", "sender", "reciever"]:
    src = (ROOT / f"{name}.html").read_text(encoding="utf-8")
    for spec in re.findall(r"""(?:href|src)="(/[^"]+)\"""", src):
        p = ROOT / spec.lstrip("/")
        ok = p.is_file()
        if not ok:
            bad += 1
        print(f"  {'OK  ' if ok else 'MISS'} {name}.html -> {spec}")

print(f"\n{'=' * 46}\n  {'FAILURES: ' + str(bad) if bad else 'all id and path checks passed'}\n{'=' * 46}")
sys.exit(1 if bad else 0)
