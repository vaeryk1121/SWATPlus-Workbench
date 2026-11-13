"""Keep installed code separate from a configured case's data and outputs."""
import json
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]


def config_root(path, cfg=None):
    path = Path(path).resolve()
    cfg = cfg if cfg is not None else json.loads(path.read_text(encoding='utf-8-sig'))
    return (path.parent/cfg.get('workspace_root', '..')).resolve()


def job_root(job, fallback=None):
    job = Path(job).resolve()
    record=job/'state.json'
    if not record.is_file():record=job/'state.archived.json'
    state = json.loads(record.read_text(encoding='utf-8-sig'))
    root = Path(state.get('workspace_root',fallback or CODE_ROOT)).resolve()
    job.relative_to(root/'runs')
    return root
