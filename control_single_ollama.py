#!/usr/bin/env python3
import sys
import os
import signal
from pathlib import Path
import subprocess


import glob
from typing import List, Tuple

EXCLUDED_CONFIG_MODULES = frozenset({
    "config_base",
    "config_sample",
    "config_loader",
    "config_test",
})


def get_pid_file(config_name: str) -> str:
    return f"/tmp/discord_ollama_{config_name}.pid"


def normalize_config_name(name: str) -> str:
    cleaned = Path(name).stem
    if not cleaned.startswith("config_"):
        candidate = f"config_{cleaned}"
        here = Path(__file__).resolve().parent
        if (here / "ollama_bot" / f"{candidate}.py").exists():
            return candidate
    return cleaned


def find_available_configs() -> List[str]:
    """base, sample, loader, test等を除外した実在する config_*.py の一覧を取得する。"""
    here = Path(__file__).resolve().parent
    ollama_bot_dir = here / "ollama_bot"
    if not ollama_bot_dir.is_dir():
        return []

    configs: List[str] = []
    for p in sorted(ollama_bot_dir.glob("config_*.py")):
        mod_name = p.stem
        if mod_name not in EXCLUDED_CONFIG_MODULES:
            configs.append(mod_name)
    return configs


def is_running(config_name: str) -> Tuple[bool, int]:
    pid_file = get_pid_file(config_name)
    if not os.path.exists(pid_file):
        return False, 0
    try:
        pid = int(Path(pid_file).read_text(encoding="utf-8").strip() or "0")
        if pid <= 0:
            return False, 0
        os.kill(pid, 0)
        return True, pid
    except (ValueError, ProcessLookupError):
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return False, 0
    except PermissionError:
        # 他ユーザーのプロセスの可能性
        return True, pid


def start(config_name: str):
    config_name = normalize_config_name(config_name)
    running, pid = is_running(config_name)
    if running:
        print(f"Ollama bot ({config_name}) is already running (PID {pid}).")
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
    
    Path(get_pid_file(config_name)).write_text(str(proc.pid), encoding="utf-8")
    print(f"Ollama bot started with PID {proc.pid} (config={config_name})")


def start_all():
    configs = find_available_configs()
    if not configs:
        print("No valid bot configs found in ollama_bot/.")
        return

    print(f"Starting all available bot configs ({len(configs)} found): {', '.join(configs)}")
    for cfg in configs:
        start(cfg)


def stop(config_name: str):
    config_name = normalize_config_name(config_name)
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
    pid_files = glob.glob("/tmp/discord_ollama_*.pid")
    if not pid_files:
        print("No running Ollama bot instances found in /tmp.")
        return

    for pf in pid_files:
        name = Path(pf).stem.replace("discord_ollama_", "")
        if name:
            stop(name)


def status():
    available = find_available_configs()
    running_instances: dict[str, int] = {}

    for pf in glob.glob("/tmp/discord_ollama_*.pid"):
        name = Path(pf).stem.replace("discord_ollama_", "")
        active, pid = is_running(name)
        if active:
            running_instances[name] = pid

    print("=== Ollama Discord Bot Status ===")
    if not available and not running_instances:
        print("No bot configs or running instances found.")
        return

    all_names = sorted(set(available) | set(running_instances.keys()))
    for name in all_names:
        if name in running_instances:
            print(f"  ● {name}: RUNNING (PID {running_instances[name]})")
        else:
            print(f"  ○ {name}: STOPPED")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: ./control_single_ollama.py [start|stop|start-all|stop-all|status] [config_name]")
        sys.exit(1)

    cmd = sys.argv[1].lower().replace("_", "-")

    if cmd in ("stop-all",):
        stop_all()
    elif cmd in ("start-all",):
        start_all()
    elif cmd in ("status",):
        status()
    elif cmd == "start":
        if len(sys.argv) < 3:
            print("Usage: ./control_single_ollama.py start <config_name|all>")
            sys.exit(1)
        target = sys.argv[2]
        if target.lower() in ("all", "all-bots"):
            start_all()
        else:
            start(target)
    elif cmd == "stop":
        if len(sys.argv) < 3 or sys.argv[2].lower() in ("all", "all-bots"):
            stop_all()
        else:
            stop(sys.argv[2])
    else:
        print("Unknown command. Use start|stop|start-all|stop-all|status.")


        
