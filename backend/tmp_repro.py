import asyncio
import sys
import traceback

sys.path.insert(0, ".")


async def main() -> None:
    from app.services.canvas_runner import run_single_node

    try:
        task = await run_single_node(1, "wf1")
        print("OK task:", task.id, task.kind, task.status)
    except Exception:
        traceback.print_exc()


asyncio.run(main())
