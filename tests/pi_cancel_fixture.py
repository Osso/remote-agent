"""Executable cancellation callback observing real host process/state boundaries."""

import json
import sys


def configure_hook(worker, repo, home, directory, pids=()):
    hook = home / "host wrappers/cancel-pi"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        f"""#!{sys.executable}
import json, sys
from pathlib import Path
home = Path({str(home)!r})
directory = Path({str(directory)!r})
def alive(pid):
    try:
        return Path(f'/proc/{{pid}}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False
with (home / 'cancel-calls').open('a') as output:
    output.write(json.dumps({{'argv': sys.argv[1:],
        'record': json.loads((directory / 'session.json').read_text()),
        'alive': [alive(pid) for pid in {list(pids)!r}]}}) + '\\n')
if (home / 'cancel-fail').exists():
    print('fixture cleanup refused', file=sys.stderr)
    sys.exit(9)
"""
    )
    hook.chmod(0o700)
    worker.projects[repo]["pi_cancel_executable"] = str(hook)
    return hook


def hook_calls(home):
    path = home / "cancel-calls"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )
