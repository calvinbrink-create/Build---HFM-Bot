#!/usr/bin/env python3
import asyncio
import os
import runpy
import sys
import traceback

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

os.chdir(os.path.dirname(os.path.abspath(__file__)))

sys.argv[0] = os.path.abspath("mt5_bot.py")
try:
    runpy.run_path(os.path.abspath("mt5_bot.py"), run_name="__main__")
except SystemExit as e:
    raise
except Exception as exc:
    print(f"[run_mt5_bot] FATAL CRASH: {exc}")
    traceback.print_exc()
    sys.exit(1)
