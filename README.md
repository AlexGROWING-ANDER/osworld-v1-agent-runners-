# Mobile-Agent-v3 on OSWorld v1

Windows runners and runtime adapters for the OSWorld v1 desktop experiments.
The GPT profile uses GPT-5.6 Sol with medium reasoning. The Qwen profile
uses Qwen3.7 Plus. The run limit is 30 steps.

| Path | Purpose |
| --- | --- |
| `runner/mobile_qwen_worker.py` | Runs a single task and records actions, screenshots and evaluator output |
| `runner/run_guarded_mobile.py` | Applies a process deadline to one task |
| `runner/run_allocation.py` | Runs the GPT task allocation supplied to the second host |
| `runner/overnight_mobile_qwen.py` | Historical Qwen batch supervisor |
| `hotkey_patch/` | The v1 rerun fixes for shortcut keys, asset downloads and PyDrive transport |
| `os_review/` | Historical v1 rerun launchers with local path defaults |
| `upstream/osworld-v1.patch` | Local setup and result-logging changes over the pinned OSWorld commit |

## Setup

Use Windows, VMware Workstation, and a local OSWorld v1 guest with the
`init_state` snapshot. The second-host experiment used Python 3.12.9.
The dependency versions from that deployment are in
`runner/requirements.lock.txt`.

Clone [OSWorld](https://github.com/xlang-ai/OSWorld) separately and check out
`b138d348256078fa634fc3b73567a7337c793e6b`. Set `OSWORLD_V1_ROOT` to the
absolute checkout path before applying the patch.

```powershell
$repoPath = (Get-Location).Path
git -C $env:OSWORLD_V1_ROOT apply (Join-Path $repoPath 'upstream/osworld-v1.patch')
```

Set `OSWORLD_VMX` to the absolute guest VMX path. Set `OSWORLD_MODEL_ENV`
to the absolute path of a local file based on `.env.example`.
Configure any task-specific assets and test accounts in the local environment.

To run one GPT task with the v1 runtime patches, from the repository root:

```powershell
$env:PYTHONPATH = (Resolve-Path './hotkey_patch').Path
python -u -X utf8 runner/run_guarded_mobile.py --deadline-seconds 2700 `
  --version v1 --model-profile gpt --steps 30 --api-timeout-seconds 240 `
  --normalized-grounding --task 'os/<task-uuid>' --output (Join-Path $repoPath 'runs/attempt_001')
```

Use a fresh output directory for each attempt. The runner writes the official
score to `result.txt` when evaluation succeeds. The allocation launcher accepts
a local manifest with `status` set to `frozen` and an array of `tasks` containing
`task_id` entries. The historical rerun launchers require the local paths and
case lists declared in their source.

## Source version

`source_manifest.json` records the hashes of the local source files and the
files in this repository. The current worker matches the latest local backup.
Historical trajectories do not carry a complete source fingerprint.

The repository adds local environment path overrides and resolves the v1
hotkey patch from its own directory. During shortcut reruns, Python loads
the pinned OSWorld Mobile-Agent module during startup. Direct runs load the
included desktop Mobile-Agent sources through the worker's import path.
Credentials, task allocations, VM images, and experiment outputs are stored
separately.
