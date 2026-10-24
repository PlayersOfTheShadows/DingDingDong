"""Offline smoke test: load every cog, register all commands, report problems.
No network access — safe to run anytime.

Run with:  .venv\\Scripts\\python.exe tools\\smoke_test.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot.main as m  # noqa: E402


async def main() -> int:
    bot = m.DingBot()
    extensions = ("bot.cogs.music", "bot.cogs.playlists", "bot.cogs.settings", "bot.cogs.ai")
    for ext in extensions:
        await bot.load_extension(ext)
        print(f"loaded {ext}")

    print()
    print("cogs:", sorted(bot.cogs))

    cmds = sorted(c.name for c in bot.commands)
    names = set(cmds)
    print(f"\nprefix+hybrid commands ({len(cmds)}):")
    print("  " + ", ".join(cmds))

    tree = bot.tree.get_commands()
    print(f"\nslash commands ({len(tree)}):")
    print("  " + ", ".join(sorted(c.name for c in tree)))
    for c in tree:
        sub = getattr(c, "commands", None)
        if sub:
            print(f"  /{c.name} subcommands: {[s.name for s in sub]}")

    aliases: list[str] = []
    for c in bot.commands:
        aliases.extend(c.aliases)
    dupes = sorted({a for a in aliases if aliases.count(a) > 1})
    print(f"\naliases: {len(aliases)} total, duplicates: {dupes or 'none'}")

    clashes = sorted({a for a in aliases if a in bot.commands})
    print("alias-vs-command-name clashes:", clashes or "none")

    await bot.close()
    print("\nSMOKE TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
