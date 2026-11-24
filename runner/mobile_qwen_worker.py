"""Isolated local runner; reuses supplied Mobile-Agent without cloud entrypoint."""
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from run_reliability import (ApiAudit, InfrastructureFailure, ResourceBlocked,
    RuntimeGuard, append_event, check_memory, memory_snapshot, normalized_to_source, journal_action)

WORKSPACE = Path(__file__).resolve().parents[1]
V1 = Path(os.environ.get('OSWORLD_V1_ROOT', 'H:/GUI_AGENT_OSWorld/OSWorld-v1'))
V2 = Path('H:/GUI_AGENT_OSWorld/OSWorld-v2.1')


def _save_screenshot(env, dest, tries: int = 8) -> bool:
    """Persist a screenshot frame, tolerating a briefly unready guest.

    Right after reset (and occasionally mid-run) the guest's /screenshot
    endpoint answers with an unexpected payload, so get_screenshot() returns
    None. The caller used to feed that straight into write_bytes(), which
    aborted the whole attempt with an opaque TypeError before the agent made
    a single call. Retry with backoff; if the frame is still missing, record
    it and let the run continue instead of dying on a bookkeeping file.
    """
    for attempt in range(1, tries + 1):
        try:
            shot = env._get_obs().get('screenshot')
        except Exception:
            shot = None
        if isinstance(shot, (bytes, bytearray)) and len(shot) > 0:
            dest.write_bytes(bytes(shot))
            return True
        if attempt < tries:
            time.sleep(min(3 * attempt, 15))
    try:
        append_event(dest.parent / 'runtime_events.jsonl',
                     {'phase': 'screenshot_missing', 'file': dest.name})
    except Exception:
        pass
    logging.warning('screenshot unavailable after %d tries: %s', tries, dest.name)
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', choices=['v1', 'v2'], required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--steps', type=int, default=30)
    parser.add_argument('--native-v2', action='store_true')
    parser.add_argument('--normalized-grounding', action='store_true')
    parser.add_argument('--wall-time-seconds', type=int, default=0)
    parser.add_argument('--runtime-budget-seconds', type=int, default=2700)
    parser.add_argument('--api-timeout-seconds', type=int, default=240)
    parser.add_argument('--api-route', choices=['environment', 'direct'], default='environment')
    parser.add_argument('--model-profile', choices=['qwen', 'gpt'], default='qwen')
    parser.add_argument('--physical-min-mb', type=int, default=None,
                        help='Minimum available physical memory in MiB')
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    guard = RuntimeGuard(Path('H:/GUI_AGENT_OSWorld/experiment_runtime.lock'))
    try:
        guard.acquire()
        resources = memory_snapshot()
        guest_mb = int(os.environ.get('OSWORLD_QEMU_MEMORY_MB', '4096')) if args.native_v2 else int(os.environ.get('OSWORLD_GUEST_MB', '4096'))
        check_memory(resources, guest_mb, args.physical_min_mb)
    except ResourceBlocked:
        guard.close()
        (out / 'task_result.json').write_text(json.dumps({'status':'blocked_resources','success':None,'resource_snapshot':locals().get('resources'), 'human_verified':False}), encoding='utf-8')
        print('Resource preflight refused start; inspect task_result.json.', flush=True)
        sys.exit(4)
    import atexit
    atexit.register(guard.close)
    audit = ApiAudit(out / 'api_events.jsonl')
    repo = V1 if args.version == 'v1' else V2
    os.chdir(repo)
    os.environ['PATH'] = 'C:/Program Files/VMware/VMware Workstation;' + os.environ['PATH']
    _extra = os.environ.get('OSWORLD_NO_PROXY_EXTRA', '').strip()
    os.environ['NO_PROXY'] = ','.join(x for x in ('localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12', _extra) if x)
    os.environ['no_proxy'] = os.environ['NO_PROXY']
    os.environ.setdefault('OSWORLD_FILE_BASE_URL', 'H:/GUI_AGENT_OSWorld/v2_assets')
    if args.version == 'v2':
        os.environ['OSWORLD_BENCHMARK_RELEASE'] = 'osworld-v2.1'
    sys.path.insert(0, str(repo))
    import mm_agents
    mm_agents.__path__.insert(0, str(WORKSPACE / 'mobile_agent_v3/os_world_v3/mm_agents'))
    from dotenv import dotenv_values
    from desktop_env.desktop_env import DesktopEnv
    from direct_cdp import install as install_direct_cdp
    install_direct_cdp()
    if args.version == 'v1':
        from recreation_navigation import install as install_recreation_navigation
        install_recreation_navigation()
    from google_session import install_login_restore, configure_google_task
    from desktop_env.controllers.setup import SetupController
    install_login_restore(SetupController)
    original_set_task = DesktopEnv._set_task_info
    def set_task_with_resolution(self, task_config):
        original_set_task(self, task_config)
        # Apply initial environment sizing BEFORE task-specific setup; do not
        # change a resolution requested by the task after setup or agent actions.
        self.setup_controller._execute_setup(command=['bash', '-c', "display=$(xrandr --query | awk '/ connected/{print $1; exit}'); xrandr --output \"$display\" --mode 1920x1080"])
        from PIL import Image
        from io import BytesIO
        size = Image.open(BytesIO(self.controller.get_screenshot())).size
        if size != (1920, 1080):
            raise RuntimeError('Initial VM resolution mismatch: ' + str(size))
    DesktopEnv._set_task_info = set_task_with_resolution
    if args.native_v2:
        import desktop_env.desktop_env as env_module
        from native_qemu_v2 import NativeQemuProvider
        provider = NativeQemuProvider(out / 'guest')
        env_module.create_vm_manager_and_provider = lambda *a, **kw: (None, provider)
    from desktop_env.providers.vmware.provider import VMwareProvider
    import subprocess
    def checked_vm_command(command, return_output=False):
        completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        if completed.returncode or completed.stdout.strip().startswith('Error:'):
            raise RuntimeError('VMware command failed: ' + completed.stdout.strip() + completed.stderr.strip())
        return completed.stdout.strip() if return_output else None
    VMwareProvider._execute_command = staticmethod(checked_vm_command)
    from mm_agents.mobileagent_v3.mobile_agent import MobileAgentV3
    from mm_agents.mobileagent_v3.mobile_agent_modules import LMMEngineOpenai
    if args.version == 'v2':
        from mm_agents.mobileagent_v3 import mobile_agent
        from mobile_hotkey_fix import install as install_mobile_hotkey_fix
        install_mobile_hotkey_fix(mobile_agent)
    if args.normalized_grounding:
        from mm_agents.mobileagent_v3.mobile_agent_modules import Grounding
        import re
        def normalized_predict(self, ref_expr, image_list):
            agent = self._create_agent()
            prompt = ('Query:' + ref_expr + '\nReturn only (x, y), using the 0 to 1000 normalized coordinate space of the entire screenshot.\n')
            agent.add_message(text_content=prompt, image_content=image_list, role='user')
            response = agent.get_response()
            agent.add_message(text_content=response, role='assistant')
            values = re.findall(r'-?\d+(?:\.\d+)?', response)
            if len(values) != 2:
                raise ValueError('Expected exactly two normalized coordinates')
            x, y = map(float, values)
            if not (0 <= x <= 1000 and 0 <= y <= 1000):
                raise ValueError('Normalized grounding coordinates out of range')
            # Supplied converter subsequently multiplies by1920/1932,1080/1092.
            return normalized_to_source(x, y), agent.messages
        Grounding.predict = normalized_predict
    from openai import OpenAI
    if args.model_profile == 'gpt':
        secret = dotenv_values(os.environ.get('OSWORLD_MODEL_ENV') or WORKSPACE / 'mobile_agent_v3/mobile_v3/.env')
        params = dict(engine_type='openai', api_key=secret['MOBILE_AGENT_API_KEY'], base_url=secret['MOBILE_AGENT_BASE_URL'], model='gpt-5.6-sol')
    else:
        secret = dotenv_values(os.environ.get('OSWORLD_MODEL_ENV') or WORKSPACE / 'agent_s/config/.env')
        params = dict(engine_type='openai', api_key=secret['GROUND_API_KEY'], base_url=secret['GROUND_URL'], model='qwen3.7-plus')
    def sanitize_messages(messages):
        import copy
        journal = copy.deepcopy(messages)
        for message in journal:
            if isinstance(message.get('content'), list):
                message['content'] = [c if c.get('type') != 'image_url' else {'type':'text','text':'[screenshot payload stored separately]'} for c in message['content']]
        return journal
    # Bound transport waits; keep prompts, decoding parameters and model unchanged.
    transport_clients = {}
    from bounded_api_transport import BoundedApiTransport
    def close_transports():
        for client in transport_clients.values():
            client.close()
    atexit.register(close_transports)
    def bounded_init(self, base_url=None, api_key=None, model=None, **kwargs):
        self.model, self.api_key, self.base_url = model, api_key, base_url
        # Modules are serial within a task. Keep their HTTP connections alive
        # instead of creating a fresh connection pool for each prediction.
        transport_key = (base_url, api_key, args.api_timeout_seconds)
        if transport_key not in transport_clients:
            transport_clients[transport_key] = BoundedApiTransport(base_url, api_key, args.api_timeout_seconds,
                                                                  trust_env=args.api_route == 'environment')
        self.llm_client = transport_clients[transport_key]
    LMMEngineOpenai.__init__ = bounded_init
    call_count = 0
    def compatible_generate(self, messages, temperature=0.0, top_p=0.8, max_new_tokens=512, **kwargs):
        nonlocal call_count
        metadata = {'model':self.model, 'request_bytes':len(json.dumps(messages).encode('utf-8')), 'image_count':sum(c.get('type')=='image_url' for m in messages for c in (m.get('content') if isinstance(m.get('content'),list) else [])), 'timeout_seconds':args.api_timeout_seconds}
        request = dict(model=self.model, messages=messages)
        if args.model_profile == 'gpt':
            request.update(reasoning_effort='medium', max_completion_tokens=8192)
            metadata['reasoning_effort'] = 'medium'
        else:
            request.update(temperature=temperature, top_p=top_p, max_tokens=max_new_tokens or 2048)
        metadata.update(api_route=args.api_route, total_request_deadline_seconds=args.api_timeout_seconds)
        response = audit.call(lambda: self.llm_client.create(**request), metadata)
        call_count += 1
        journal = sanitize_messages(messages)
        (out / f'llm_call_{call_count:04d}.json').write_text(json.dumps({'messages':journal, 'response':response.choices[0].message.content, 'finish_reason':response.choices[0].finish_reason, 'usage':response.usage.model_dump() if response.usage else None}, ensure_ascii=False), encoding='utf-8')
        return response.choices[0].message.content
    LMMEngineOpenai.generate = compatible_generate
    from mm_agents.mobileagent_v3 import mobile_agent_modules as modules
    def wrap_predict(cls, role):
        original = cls.predict
        def predict(self, *values, **kwargs):
            previous = audit.module
            audit.module = role
            try:
                value = original(self, *values, **kwargs)
                folder = out / f'step_{audit.step}'
                folder.mkdir(exist_ok=True)
                (folder / f'{role}.json').write_text(json.dumps({'name':role,'response':value[0], 'messages':sanitize_messages(value[1])}, ensure_ascii=False, default=str),encoding='utf-8')
                append_event(out / 'module_events.jsonl', {'step':audit.step,'module':role,'phase':'completed'})
                return value
            except Exception as exc:
                append_event(out / 'module_events.jsonl', {'step':audit.step,'module':role,'phase':'failed','error_type':type(exc).__name__})
                raise
            finally:
                audit.module = previous
        cls.predict = predict
    for cls, role in [(modules.Manager,'manager'),(modules.Executor,'operator'),(modules.Grounding,'grounding'),(modules.Reflector,'reflector')]:
        wrap_predict(cls, role)
    logging.basicConfig(level=logging.INFO)
    if args.version == 'v1':
        domain, task_id = args.task.split('/')
        task = json.loads((V1 / f'evaluation_examples/examples/{domain}/{task_id}.json').read_text(encoding='utf-8'))
        google_session_restore = configure_google_task(task)
        instruction = task['instruction']
    else:
        import importlib
        if not args.task.isdigit() or len(args.task) != 3:
            raise ValueError('V2 task must be its three-digit release task ID')
        try:
            module = importlib.import_module('evaluation_examples.task_class.task_' + args.task)
            task = getattr(module, 'Task' + args.task)()
        except Exception as exc:
            # Import-time configuration errors used to escape before the normal
            # result writer, leaving an empty attempt and an unexplained exit 1.
            # Preserve a machine-readable result without serializing secrets or
            # arbitrary exception text.
            missing_gitlab = (
                type(exc).__name__ == 'ValueError'
                and 'GITLAB_URL and GITLAB_PRIVATE_TOKEN' in str(exc)
            )
            preflight_result = {
                'task': args.task,
                'version': args.version,
                'architecture': 'mobile_agent_v3',
                'status': 'blocked_configuration' if missing_gitlab else 'incomplete',
                'success': None,
                'human_verified': False,
                'error_type': type(exc).__name__,
                'failure_stage': 'task_import',
            }
            if missing_gitlab:
                preflight_result.update(
                    failure_category='missing_official_credentials',
                    missing_variables=['GITLAB_URL', 'GITLAB_PRIVATE_TOKEN'],
                )
            (out / 'task_result.json').write_text(
                json.dumps(preflight_result, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            print(json.dumps(preflight_result, ensure_ascii=False), flush=True)
            sys.exit(3 if missing_gitlab else 2)
        if task.id != args.task:
            raise ValueError('Loaded V2 task identity mismatch')
        instruction = task.instruction
    result = {'task': args.task, 'version': args.version, 'architecture': 'mobile_agent_v3', 'models': {k: params['model'] for k in ['manager','operator','reflector','grounding']}, 'reasoning_effort': 'medium' if args.model_profile == 'gpt' else None, 'formal_benchmark': args.version == 'v1', 'guest': 'V1 VMware', 'max_steps': args.steps, 'compatibility_changes': ['base64 image transport from supplied source', 'omit rejected vLLM top_k=-1 and skip_special_tokens extras', '120s request timeout and one SDK retry']}
    if args.version == 'v2':
        result['benchmark_release'] = 'osworld-v2.1'
        result['task_source_sha256'] = __import__('hashlib').sha256((V2 / ('evaluation_examples/task_class/task_' + args.task + '.py')).read_bytes()).hexdigest()
    env = None
    result['process_runtime_budget_seconds']=args.runtime_budget_seconds
    if args.version == 'v1' and google_session_restore:
        result['compatibility_changes'].append('Google-only Windows-user-encrypted cookies restored after original snapshot; no password file; explicit VM proxy')
    result.update(success=None, human_verified=False, resource_snapshot=resources,
        api_timeout_seconds=args.api_timeout_seconds, api_max_attempts=3, wall_time_seconds=args.wall_time_seconds,
        physical_min_mb=args.physical_min_mb if args.physical_min_mb is not None else guest_mb + 2500)
    result['compatibility_changes'][-1] = 'explicit call-only transient retries (max3); resource gate and action journal'
    if args.version == 'v2':
        result['compatibility_changes'].append('normalize Mobile-Agent hotkey lists before action dispatch')
    result['compatibility_changes'].append('reuse task-local HTTP connection pool across serial model modules')
    result['compatibility_changes'].append('private-VM evaluator CDP discovery bypasses host proxy; model API unchanged')
    if args.version == 'v1' and args.task == 'chrome/b4f95342-463e-4179-8c3f-193cd7241fb2':
        result['compatibility_changes'].append('Recreation expected getter uses DOM-ready navigation waits; campground, Next Available and score conditions unchanged')
    if args.normalized_grounding:
        result['compatibility_changes'].append('explicit 0-1000 grounding prompt and normalized-to-pixel coordinate conversion')
    try:
        if args.version == 'v1':
            from setup_asset_cache import prepare, missing_credentials
            missing = missing_credentials(task, repo)
            if missing:
                result.update(status='blocked_configuration', missing_files=missing,
                              failure_category='missing_official_credentials')
                raise ResourceBlocked('Missing official task credentials')
            prepare(task, out / 'cache')
        if args.native_v2:
            result.update(guest='pinned official V2 qcow2, SHA256 verified', provider='native Windows QEMU (not pinned Docker runtime)', formal_benchmark=False, official_v2_image=True)
            result['guest_memory_mb'] = int(os.environ.get('OSWORLD_QEMU_MEMORY_MB', '4096'))
        environment_type = DesktopEnv
        if args.version == 'v1' and not args.native_v2:
            from reset_first_environment import reset_first_environment
            environment_type = reset_first_environment(DesktopEnv)
            result['compatibility_changes'].append('defer unused constructor boot until official snapshot reset')
        initialization_started = time.monotonic()
        snapshot_options = {}
        if args.version == 'v1' and not args.native_v2 and google_session_restore:
            auth_snapshot = os.environ.get('OSWORLD_V1_AUTH_SNAPSHOT')
            if auth_snapshot:
                if auth_snapshot != 'osworld_v1_google_authenticated_20261118':
                    raise ValueError('Unexpected authenticated v1 snapshot')
                snapshot_options['snapshot_name'] = auth_snapshot
                result['compatibility_changes'].append('original init_state plus verified Google test-account login, restored as authenticated VM snapshot')
        env = environment_type(provider_name='docker' if args.native_v2 else 'vmware', path_to_vm=str(provider.base) if args.native_v2 else os.environ.get('OSWORLD_VMX', 'H:/GUI_AGENT_OSWorld/Ubuntu_overnight_20261008/Ubuntu0.vmx'), headless=True, client_password='osworld-public-evaluation' if args.native_v2 else 'password', require_a11y_tree=False, cache_dir=str(out / 'cache'), **snapshot_options)
        obs = env.reset(task)
        result['initialization_seconds'] = round(time.monotonic() - initialization_started, 3)
        append_event(out / 'runtime_events.jsonl', {'phase':'environment_ready', 'elapsed_seconds':result['initialization_seconds']})
        time.sleep(15)
        _save_screenshot(env, out / 'step_0.png')
        agent = MobileAgentV3(params.copy(), params.copy(), params.copy(), params.copy())
        settings = SimpleNamespace(enable_rag=0, rag_path='', guide_path='', grounding_stage=1, grounding_info_level=1, max_trajectory_length=args.steps)
        env.controller.start_recording()
        original_env_step = env.step
        def journaled_env_step(action, *values, **kwargs):
            folder = out / f'step_{audit.step}'
            return journal_action(folder, out / 'action_events.jsonl',audit.step, action,
                env.controller.get_screenshot, lambda: original_env_step(action, *values, **kwargs))
        env.step = journaled_env_step
        action_started = time.monotonic()
        for step in range(1, args.steps + 1):
            audit.step = step
            audit.last_failure = None
            if args.wall_time_seconds and time.monotonic() - action_started >= args.wall_time_seconds:
                result['termination_reason'] = 'diagnostic_wall_time_limit'
                result['formal_benchmark'] = False
                env.step('FAIL', 0)
                break
            state, action, valid, reward, done = agent.step(instruction, env, settings)
            _save_screenshot(env, out / f'step_{step}.png')
            (out / f'step_{step}').mkdir(exist_ok=True)
            for module, data in state.items():
                # Screenshots are separate files, not repeated base64 in logs.
                if isinstance(data, dict):
                    for message in data.get('messages') or []:
                        if isinstance(message, dict) and isinstance(message.get('content'), list):
                            message['content'] = [c if not isinstance(c, dict) or c.get('type') != 'image_url' else {'type':'text','text':'[image saved as step screenshot]'} for c in message['content']]
                (out / f'step_{step}' / f'{module}.json').write_text(json.dumps(data, ensure_ascii=False, default=str), encoding='utf-8')
            with (out / 'traj.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(dict(step_num=step, action=action, done=done, step_status=valid, screenshot_file=f'step_{step}.png'), ensure_ascii=False) + '\n')
            if not valid:
                if audit.last_failure is not None:
                    raise audit.last_failure
                result['termination_reason'] = 'agent_invalid_action'
                env.step('FAIL', 0)
                break
            if done:
                break
        result.update(status='completed', steps=step, agent_done=done, score=env.evaluate())
        result['human_verified'] = False
        result['success'] = bool(result['score'] == 1) if result.get('termination_reason') != 'diagnostic_wall_time_limit' else None
        if result.get('termination_reason') == 'diagnostic_wall_time_limit':
            result['status'] = 'diagnostic_timeout'
        result['api_timeout_seconds'] = args.api_timeout_seconds
        result['wall_time_seconds'] = args.wall_time_seconds
        (out / 'result.txt').write_text(str(result['score']), encoding='utf-8')
    except Exception as exc:
        # No secrets or raw exception bodies in summary.
        if result.get('status') != 'blocked_configuration':
            result.update(status='incomplete', error_type=type(exc).__name__)
        result['exception_chain'] = __import__('run_reliability').error_chain(exc)
        import traceback
        # Full message/traceback stays out of the summary (it may echo tokens or
        # URLs) but is written next to the attempt so a crash is diagnosable
        # without re-running the task. Local file only.
        try:
            (out / 'error_detail.txt').write_text(
                ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                encoding='utf-8', errors='replace')
        except Exception:
            pass
        frames=[]
        current=exc
        while current is not None:
            frames.append({'type':type(current).__name__,'frames':[{'file':f.filename,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(current.__traceback__)]})
            current=current.__cause__ or current.__context__
            if len(frames)>=8:break
        result['exception_frames']=frames
        if isinstance(exc, InfrastructureFailure):
            result.update(status='infrastructure_error', failure_category=exc.category, failure_module=exc.module, success=None)
        logging.error('Task stopped: %s', type(exc).__name__)
        import traceback
        traceback.print_tb(exc.__traceback__)
    finally:
        if env:
            try:
                env.controller.end_recording(str(out / 'recording.mp4'))
            except Exception:
                pass
            if args.native_v2:
                try:
                    env.close()
                except Exception as exc:
                    result.setdefault('cleanup_errors', []).append(type(exc).__name__)
        if args.native_v2:
            # Constructor can fail after QEMU starts, before env is assigned.
            try:
                provider.stop_emulator(str(provider.base))
            except Exception as exc:
                result.setdefault('cleanup_errors', []).append(type(exc).__name__)
        (out / 'task_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)
    sys.exit(0 if result['status'] == 'completed' else 3 if result.get('error_type') in ['AuthenticationError', 'PermissionDeniedError'] else 2)

if __name__ == '__main__':
    main()
