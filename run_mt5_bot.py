#!/usr/bin/env python3
import asyncio
import fcntl
import os
import runpy
import sys
import traceback
from pathlib import Path

LOCK_PATH = Path("/run/lock/cipherfx-mt5-bot.lock")

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

os.chdir(os.path.dirname(os.path.abspath(__file__)))

LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
_process_lock = LOCK_PATH.open("a+")
try:
    fcntl.flock(_process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    _process_lock.seek(0)
    owner = _process_lock.read().strip() or "unknown"
    print(f"[run_mt5_bot] STARTUP_REFUSED process lock held by pid={owner}", flush=True)
    sys.exit(73)
_process_lock.seek(0)
_process_lock.truncate()
_process_lock.write(str(os.getpid()))
_process_lock.flush()
os.fchmod(_process_lock.fileno(), 0o600)

sys.argv[0] = os.path.abspath("mt5_bot.py")
try:
    runpy.run_path(os.path.abspath("mt5_bot.py"), run_name="__main__")
except SystemExit as e:
    raise
except Exception as exc:
    print(f"[run_mt5_bot] FATAL CRASH: {exc}")
    traceback.print_exc()
    sys.exit(1)
