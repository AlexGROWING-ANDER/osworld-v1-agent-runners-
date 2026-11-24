"""Whole-process deadline; preserve worker evidence and mark incomplete on timeout."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--deadline-seconds', type=int, default=1800)
    options, worker_args = parser.parse_known_args()
    if options.deadline_seconds <= 0 or '--output' not in worker_args:
        parser.error('Positive deadline and worker --output are required')
    output = Path(worker_args[worker_args.index('--output') + 1])
    if output.exists():
        parser.error('Output already exists; refusing overwrite')
    child = subprocess.Popen([sys.executable, '-u', '-X', 'utf8', str(Path(__file__).with_name('mobile_qwen_worker.py')), *worker_args], creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + options.deadline_seconds
    while child.poll() is None:
        if time.monotonic() >= deadline:
            subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'], capture_output=True)
            child.wait(timeout=30)
            output.mkdir(parents=True, exist_ok=True)
            result = output / 'task_result.json'
            if not result.exists():
                result.write_text(json.dumps({'status':'watchdog_timeout','success':None,'human_verified':False,'termination_reason':'whole_process_deadline','deadline_seconds':options.deadline_seconds,'action_state':'inspect action_events.jsonl; never automatically replay an uncertain action'},indent=2),encoding='utf-8')
            return 2
        time.sleep(1)
    return child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
