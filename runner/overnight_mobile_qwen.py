"""Bounded sequential supervisor. No API credentials in process arguments."""
import json
import subprocess
import sys
import time
import ctypes
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path('E:/GUI_AGENT_runs/overnight_mobile_qwen') / datetime.now().strftime('%Y%m%d_%H%M%S')

def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + 8 * 3600
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    state = {'pid': __import__('os').getpid(), 'started': datetime.now().isoformat(), 'hours': 8, 'status': 'running', 'events': [], 'root': str(ROOT)}
    def save() -> None:
        state['heartbeat'] = datetime.now().isoformat()
        temp = ROOT / 'status.tmp'
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(ROOT / 'status.json')
    def run(version: str, task: str, attempts: int = 2) -> bool:
        for attempt in range(attempts):
            if time.monotonic() >= deadline or (ROOT / 'STOP').exists():
                return False
            output = ROOT / version / task.replace('/', '_') / f'attempt_{attempt+1}'
            output.parent.mkdir(parents=True, exist_ok=True)
            state.update(current_task=task, version=version, attempt=attempt+1)
            with (output.parent / f'attempt_{attempt+1}.log').open('w', encoding='utf-8') as log:
                import os
                child_env = os.environ.copy()
                child_env['PYTHONUNBUFFERED'] = '1'
                child_env['PYTHONIOENCODING'] = 'utf-8'
                child = subprocess.Popen([sys.executable, '-X', 'utf8', str(HERE / 'run_guarded_mobile.py'), '--deadline-seconds', str(max(1,min(39*60,int(deadline-time.monotonic())-30))), '--version', version, '--task', task, '--output', str(output), '--steps', '3' if version == 'v2' else '30'], stdout=log, stderr=log, env=child_env)
                state['worker_pid'] = child.pid
                save()
                end = min(deadline, time.monotonic() + 40*60)
                while child.poll() is None and time.monotonic() < end:
                    save()
                    if (ROOT / 'STOP').exists():
                        break
                    time.sleep(15)
                if child.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True)
                    code = -1
                else:
                    code = child.returncode
            state['events'].append({'task':task, 'version':version, 'attempt':attempt+1, 'exit_code':code, 'output':str(output)})
            save()
            if code == 3:
                state['fatal_api_auth'] = True
                return False
            if code == 4:
                state['resource_blocked'] = True
                return False
            if code == 0:
                return True
            time.sleep(15)
        return False
    try:
        # V2 compatibility pilot is isolated and never counted as formal V2.
        proof = Path('E:/GUI_AGENT_runs/osworld_v2/compatibility_mobile_qwen_20261008_0216/task_result.json')
        previous = json.loads(proof.read_text(encoding='utf-8')) if proof.exists() else {}
        state['v2_pilot_completed'] = previous.get('status') == 'completed' or run('v2', '004', attempts=1)
        if not state['v2_pilot_completed']:
            state['status'] = 'stopped_v2_pilot_incomplete'
            return
        preflight = 'os/bedcedc4-4d72-425e-ad62-21960b11fe0d'
        if not run('v1', preflight):
            state['status'] = 'stopped_preflight_incomplete'
            return
        manifest = json.loads(Path('H:/GUI_AGENT_OSWorld/OSWorld-v1/evaluation_examples/test_all.json').read_text(encoding='utf-8'))
        tasks = [f'{domain}/{tid}' for domain in ['os'] + [x for x in manifest if x != 'os'] for tid in manifest[domain] if f'{domain}/{tid}' != preflight]
        state['v1_total_tasks'] = len(tasks)+1
        failures = 0
        for task in tasks:
            if state.get('resource_blocked'):
                state['status'] = 'stopped_resource_gate'
                break
            if state.get('fatal_api_auth') or (ROOT / 'STOP').exists():
                state['status'] = 'stopped_auth_or_user_stop'
                break
            if time.monotonic() >= deadline:
                state['status'] = 'eight_hour_window_ended'
                break
            if __import__('shutil').disk_usage(ROOT).free < 3*1024**3:
                state['status'] = 'stopped_low_disk'
                break
            completed = run('v1', task)
            failures = 0 if completed else failures+1
            if failures >= 3:
                state['status'] = 'stopped_repeated_incomplete_tasks'
                break
        else:
            state['status'] = 'all_v1_tasks_attempted'
    finally:
        state['finished'] = datetime.now().isoformat()
        save()
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

if __name__ == '__main__':
    main()
