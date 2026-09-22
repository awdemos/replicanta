"""Shared bootstrap for the manual doom smoke-test probe.

Builds a throwaway nursery organism with the doom-ascii module enabled and
yields the (tmp, org, app) triple, ready to be driven via Textual's
run_test() pilot.
"""

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from replicanta import nursery as nursery_mod
from replicanta.organism import Organism
from replicanta.tui import OrganismApp


@contextmanager
def make_test_org():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        seed = tmp / "organism.scl"
        seed.write_text("type bel(x: String, a: String, v: String)\n")
        nursery_mod.create(tmp, "doomtest", seed)
        shutil.copytree(Path(__file__).parent.parent / "modules", tmp / "modules")
        (tmp / "replicanta.toml").write_text('[modules]\nenabled = ["base", "doom-ascii"]\n')
        org = Organism(nursery_mod.organism_dir(tmp, "doomtest"))
        org.load()
        app = OrganismApp(org, root=tmp)
        yield tmp, org, app
