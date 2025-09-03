import os
import time
import shlex
import subprocess
from datetime import datetime


def is_15m_close(now: datetime) -> bool:
    return now.minute % 15 == 0 and now.second >= 10


def main():
    # Fast tick every 10s, but only run run.py at 15m close + ~10s
    tick = int(os.getenv("TICK_SECONDS", "10"))
    flags = os.getenv("RUN_FLAGS", "--execute --margin CROSSED --override-direction both")
    cmd = f"python run.py {flags}".strip()
    print(f"[loop] {datetime.now().isoformat()} start tick={tick}s cmd='{cmd}'", flush=True)
    while True:
        try:
            now = datetime.utcnow()
            if is_15m_close(now):
                print(f"[loop] {datetime.now().isoformat()} 15m close → running", flush=True)
                proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
                print(f"[loop] {datetime.now().isoformat()} exit={proc.returncode}", flush=True)
                if proc.stdout:
                    print(proc.stdout, flush=True)
                if proc.stderr:
                    print(proc.stderr, flush=True)
            else:
                # placeholder: management loop could go here
                pass
        except Exception as e:
            print(f"[loop] {datetime.now().isoformat()} exception: {e}", flush=True)
        time.sleep(tick)


if __name__ == "__main__":
    main()
