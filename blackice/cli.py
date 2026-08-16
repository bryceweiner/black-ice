from __future__ import annotations

import argparse
import contextlib
import getpass
import signal
import sys

from . import logging_setup
from .config import get_settings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="blackice")
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--reload", action="store_true")

    sub.add_parser("hash-password", help="hash a password for ADMIN_PASSWORD_HASH")
    sub.add_parser("initdb", help="create the database and data directories")
    sub.add_parser("voice-check", help="report whether the voice stack is ready")

    voice = sub.add_parser("voice", help="run the voice loop on its own")
    voice.add_argument("--say", help="speak a line and exit")

    consol = sub.add_parser("consolidate", help="run memory consolidation now")
    consol.add_argument("--hours", type=int, default=24)

    args = p.parse_args(argv)
    s = get_settings()
    logging_setup.configure()

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "blackice.api.app:app",
            host=s.host,
            port=s.port,
            reload=args.reload,
            log_level="info",
        )
        return 0

    if args.cmd == "hash-password":
        from .api.auth import hash_password

        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Confirm: "):
            print("Passwords do not match", file=sys.stderr)
            return 1
        print(hash_password(pw))
        return 0

    if args.cmd == "initdb":
        import asyncio

        from . import db

        async def go():
            await db.connect()
            await db.close()

        asyncio.run(go())
        print(f"Initialised {s.db_path}")
        return 0

    if args.cmd == "voice-check":
        from .voice.voice2_backend import Voice2Backend

        problems = Voice2Backend.preflight()
        if not problems:
            print(f"Voice is ready. Wake word: {s.assistant_name}")
            return 0
        print("Voice is not ready:")
        for p in problems:
            print(f"  - {p}")
        return 1

    if args.cmd == "voice":
        import asyncio

        from . import db
        from .llm.coretools import register_core_tools
        from .llm.tools import project_plugin_tools
        from .llm.tools import registry as tool_registry
        from .plugins.registry import registry
        from .services import events
        from .voice.announce import Announcer
        from .voice.voice2_backend import Voice2Backend

        async def go():
            await db.connect()
            # Without these the assistant can hear you but can do nothing.
            register_core_tools()
            await registry.start_all(events.record)
            project_plugin_tools(registry, tool_registry)
            backend = Voice2Backend()
            await backend.start()
            print(f"{len(tool_registry.tools)} tools available")
            announcer = None
            try:
                if args.say:
                    await backend.say(args.say)
                    await asyncio.sleep(5)
                    return
                # The only process with a speaker, so the only one that can
                # deliver a reminder when it comes due.
                announcer = Announcer(backend)
                await announcer.start()
                print(f"Listening. Say '{s.assistant_name}' to wake. Ctrl+C to stop.")

                # Two ways out: a real signal, or voice2's raw-mode keyboard
                # worker swallowing Ctrl-C and setting its own shutdown flag.
                signalled = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    with contextlib.suppress(NotImplementedError):
                        loop.add_signal_handler(sig, signalled.set)

                waiters = [
                    asyncio.create_task(signalled.wait()),
                    asyncio.create_task(backend.wait_closed()),
                ]
                done, pending = await asyncio.wait(
                    waiters, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
            finally:
                print("\nShutting down…")
                if announcer is not None:
                    await announcer.stop()
                await backend.stop()
                await registry.stop_all()
                await db.close()

        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(go())
        return 0

    if args.cmd == "consolidate":
        import asyncio

        from . import db
        from .memory import consolidate

        async def go():
            await db.connect()
            result = await consolidate.consolidate_all(args.hours)
            await db.close()
            return result

        print(asyncio.run(go()))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
