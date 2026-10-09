"""start.sh keeps hermes temp I/O off the persisted /configs volume.

hermes at f97608f1 exports TMPDIR/TMP/TEMP=$HERMES_HOME/cache/scratch at import
unless one is already set. start.sh makes $HERMES_HOME a symlink onto /configs,
so TMPDIR must be exported before the first hermes launch.
"""
import pathlib
import re

START_SH = pathlib.Path(__file__).resolve().parents[1] / "start.sh"


def _code_lines():
    return [
        (i, line)
        for i, line in enumerate(START_SH.read_text(encoding="utf-8").splitlines())
        if not line.lstrip().startswith("#")
    ]


def test_tmpdir_exported_before_first_hermes_launch():
    lines = _code_lines()
    exports = [i for i, line in lines if re.match(r'\s*export TMPDIR="\$\{TMPDIR:-/tmp\}"\s*$', line)]
    assert exports, "start.sh no longer exports TMPDIR (defaulting to /tmp)"
    # Real launches only: `bash -c "cd /tmp && hermes gateway"` (boot and the
    # watcher relaunch) and `timeout 10 hermes mcp list`; echo lines that merely
    # mention "hermes gateway" (the smoke-mode notice) are not launches.
    launches = [
        i for i, line in lines
        if re.search(r"(&& hermes gateway\b|\btimeout \d+ hermes mcp list\b)", line)
    ]
    assert launches, "no hermes launch found in start.sh"
    assert exports[0] < launches[0], "TMPDIR must be exported before the first hermes launch"
