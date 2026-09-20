import asyncio
from pathlib import Path
import tempfile
import shutil
from textual.widgets import Input

from replicanta import nursery as nursery_mod
from replicanta.organism import Organism
from replicanta.tui import OrganismApp

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    seed = tmp / "organism.scl"
    seed.write_text("type bel(x: String, a: String, v: String)\n")
    nursery_mod.create(tmp, "doomtest", seed)
    shutil.copytree(Path(__file__).parent.parent / "modules", tmp / "modules")
    (tmp / "replicanta.toml").write_text('[modules]\nenabled = ["base", "nano-doom"]\n')
    org = Organism(nursery_mod.organism_dir(tmp, "doomtest"))
    org.load()
    app = OrganismApp(org, root=tmp)

    async def check():
        async with app.run_test() as pilot:
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "start nano-doom and take one action"
            await pilot.press("enter")
            await asyncio.sleep(6.0)
            print("--- log tail ---")
            for line in app.query_one("#dreams").lines[-20:]:
                print(str(line))
            print("--- beliefs ---")
            print("doom/frame:", org.store.belief_value("doom", "frame"))
            print("--- doom memories ---")
            for m in org.store.memory:
                if m["kind"] == "doom":
                    print(m["text"])

    asyncio.run(check())
