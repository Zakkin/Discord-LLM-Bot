#!/usr/bin/env python3
import sys
import os
import signal
from pathlib import Path
import subprocess


def get_pid_file(config_name: str) -> str:
    return f"/tmp/discord_ollama_{config_name}.pid"


def start(config_name: str):
    pid_file = get_pid_file(config_name)

    if os.path.exists(pid_file):
        print(f"Ollama bot ({config_name}) is already running.")
        return

    here = Path(__file__).resolve().parent

    # 設定ファイルの存在確認
    target_py = here / "ollama_bot" / f"{config_name}.py"
    if not target_py.exists():
        print(f"Error: Config file not found: {target_py}")
        print(f"Cannot start Ollama bot with config={config_name}.")
        sys.exit(1)

    env = dict(os.environ)

    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{here}:{pythonpath}" if pythonpath else str(here)
    env["OLLAMA_BOT_CONFIG"] = config_name

    log_path = f"/var/log/ollama_bot_{config_name}.log"
    try:
        log_fp = open(log_path, "a", encoding="utf-8")
    except OSError:
        fallback_log = here / f"ollama_bot_{config_name}.log"
        log_fp = open(fallback_log, "a", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "ollama_bot.main"],
        cwd=here,
        env=env,
        stdout=log_fp,
        stderr=log_fp,
    )
    
    Path(pid_file).write_text(str(proc.pid), encoding="utf-8")
    print(f"Ollama bot started with PID {proc.pid} (config={config_name})")


def stop(config_name: str):
    pid_file = get_pid_file(config_name)

    if not os.path.exists(pid_file):
        print(f"Ollama bot ({config_name}) is not running.")
        return

    pid = int(Path(pid_file).read_text(encoding="utf-8").strip() or "0")
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to Ollama bot ({config_name}) process with PID {pid}. Waiting for exit...")
        
        import time
        for _ in range(15):
            try:
                os.kill(pid, 0)
                time.sleep(1)
            except ProcessLookupError:
                print(f"Process {pid} has exited.")
                break
        else:
            print(f"Process {pid} did not exit after 15 seconds. Sending SIGKILL...")
            try:
                os.kill(pid, signal.SIGKILL)
                time.sleep(1)
            except ProcessLookupError:
                pass
    except ProcessLookupError:
        print("Process not found.")

    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass


def stop_all():
    import glob
    pid_files = glob.glob("/tmp/discord_ollama_*.pid")
    if not pid_files:
        print("No running Ollama bot instances found in /tmp.")
        return

    for pf in pid_files:
        name = Path(pf).stem.replace("discord_ollama_", "")
        if name:
            stop(name)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: ./control_single_ollama.py [start|stop|stop-all] [config_name]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd in ("stop-all", "stop_all"):
        stop_all()
    elif cmd == "start":
        if len(sys.argv) < 3:
            print("Usage: ./control_single_ollama.py start <config_name>")
            sys.exit(1)
        start(sys.argv[2])
    elif cmd == "stop":
        if len(sys.argv) < 3 or sys.argv[2] == "all":
            stop_all()
        else:
            stop(sys.argv[2])
    else:
        print("Unknown command. Use start|stop|stop-all.")

        
