import asyncio

from textual.widgets import Input

from _smoke_org import make_test_org


def main():
    with make_test_org() as (_, org, app):

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


if __name__ == "__main__":
    main()
