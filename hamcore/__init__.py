"""hamcore — the parts every one of these ham-radio projects needed anyway.

For years the same four things were copy-pasted between logan, log_check,
Contest_Plan, RBN and sonify: the DXCC/ITU callsign resolution chain, the
sun and grayline maths, the GFZ and NOAA space-weather readers, and an ADIF
parser. Seven copies of `dxcc.json`, four of `parse_adif_records`, two
different modules both called `solar.py`.

That is not a tidiness problem, it is a correctness one. When ten DXCC
entities turned out to be sitting at the wrong coordinates the fix had to be
made by hand in each copy — and log_check was left behind, quietly resolving
India to the South Pole for months after everywhere else was right. When
GFZ's `D` column turned out to be 0/1/2 rather than a boolean, the same
one-line bug had to be found in three files.

So: one copy, one test suite, and `vendor.py` to push it out to the projects
that need to stay standalone-clonable, with `--check` to fail loudly when a
consumer drifts instead of letting it rot.

    from hamcore import locate, sun, bands, adif
    from hamcore.solar import gfz, swpc

Pure standard library, and it stays that way.
"""
# ---------------------------------------------------------------------------
# VENDORED from hamcore 1.0.0 -- do not edit here.
# Edit AI/hamcore/hamcore/__init__.py and re-run:
#     python3 -m hamcore.vendor sync
# ---------------------------------------------------------------------------

import hashlib
import json
from pathlib import Path

__version__ = "1.0.0"

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
MANIFEST = HERE / ".hamcore-manifest.json"


def data_path(name):
    """Absolute path to a bundled data file, e.g. data_path('dxcc.json').

    The single source of truth for dxcc.json, itu.json and rare.json — the
    files that used to exist in seven, seven and five copies.
    """
    p = DATA / name
    if not p.exists():
        raise FileNotFoundError(f"hamcore data file not found: {p}")
    return p


def verify():
    """Check a *vendored* copy against the manifest it was stamped with.

    `vendor.py` is not shipped with the copies, and in a standalone clone the
    workspace it came from is not there either — so this validates the tree
    against the hashes recorded at sync time, needing nothing external. Wire it
    into a project's test suite:

        self.assertEqual(hamcore.verify(), [])

    and hand-editing a vendored file becomes a red test rather than a silent
    fork. Returns a list of problems; empty means clean. An unvendored (source)
    checkout has no manifest and reports nothing to check.
    """
    if not MANIFEST.exists():
        return []
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems = []
    for rel, hashes in sorted(man.get("files", {}).items()):
        p = HERE / rel
        if not p.exists():
            problems.append(f"missing {rel}")
            continue
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        if got != hashes.get("out"):
            problems.append(f"{rel} was edited in place "
                            f"(vendored from hamcore {man.get('version')})")
    return problems


__all__ = ["DATA", "MANIFEST", "data_path", "verify", "__version__"]
