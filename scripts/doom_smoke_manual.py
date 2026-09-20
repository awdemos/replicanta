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
            chat.value = "/doom start"
            await pilot.press("enter")
            await asyncio.sleep(0.5)
            print("log lines:", len(app.query_one("#dreams").lines))
            for i, line in enumerate(app.query_one("#dreams").lines[-12:]):
                print(i, repr(str(line))[:120])
            print("doom running:", app.org.module_loader.registry.get("doom").running())
            print("belief:", app.org.store.belief_value("doom", "frame"))
            print("memory kinds:", [(m["kind"], m["text"][:40]) for m in app.org.store.memory])

    asyncio.run(check())
