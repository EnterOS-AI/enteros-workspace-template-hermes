"""What a Light run's copy of a hermes agent's volume leaves out (contract C2).

A Light run pod's init container copies the parked agent's volume into a
private directory with molecule-runtime-light-copy, which reads this image's
list (light-copy-exclude.txt, named by MOLECULE_LIGHT_COPY_EXCLUDE_FILE). The
run must start a fresh conversation and must not do the agent's own work a
second time, and it must keep the agent's identity, credentials, skills,
memories and plugins. These tests read the list with the runtime's own
loader and matcher, check every line against a real claim, and run a real copy.
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest
import yaml

from molecule_runtime import light_copy

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXCLUDE_FILE = ROOT / "light-copy-exclude.txt"
IMAGE_PATH = "/app/light-copy-exclude.txt"

# The volume of the staging workspace "hermes v0.22.5 check M2.7" (be7540cf),
# measured on 2026-11-13 (notes/spike.md, "Volume layout"), plus what start.sh
# writes into HERMES_HOME on every boot. Paths are relative to the volume root;
# a trailing / marks a directory.
MEASURED = [
    "configs/.hermes/",
    "configs/.hermes/state.db", "configs/.hermes/state.db-wal", "configs/.hermes/state.db-shm",
    "configs/.hermes/logs/", "configs/.hermes/skills/", "configs/.hermes/sessions/", "configs/.hermes/cache/",
    "configs/.hermes/kanban.db", "configs/.hermes/kanban.db-shm",
    "configs/.hermes/response_store.db", "configs/.hermes/response_store.db-wal",
    "configs/.hermes/response_store.db-shm",
    "configs/.hermes/runs_idempotency.db", "configs/.hermes/runs_idempotency.db-wal",
    "configs/.hermes/runs_idempotency.db-shm",
    "configs/.hermes/.curator_backups/", "configs/.hermes/cron/", "configs/.hermes/SOUL.md",
    "configs/.hermes/gateway.log", "configs/.hermes/plugins/", "configs/.hermes/backups/",
    "configs/.hermes/config.yaml", "configs/.hermes/.env", "configs/.hermes/gateway_state.json",
    "configs/.hermes/source-checks/", "configs/.hermes/state/", "configs/.hermes/installs",
    # start.sh
    "configs/.hermes/molecule-mcp-server.log", "configs/.hermes/.clean_shutdown",
    "configs/.hermes/plugins/molecule_turn_activity/",
    # outside HERMES_HOME
    "configs/plugins/", "configs/config.yaml", "configs/.auth_token", "configs/.platform_inbound_secret",
    "configs/.config-relay-materialized", "configs/.molecule", "configs/.molecule_plugins_boot_state.json",
    "configs/skills/",
    "rtstate/workspace/.molecule/chat-uploads/", "rtstate/workspace/.molecule/memory/",
    "rtstate/workspace/.molecule/idle-prompt", "rtstate/home/agent/.hermes/auth/",
    "rtstate/home/agent/.molecule/plugin-state/",
]

# Lines of the list that match nothing on the measured claim, and the hermes code
# (at the pinned HERMES_COMMIT) that writes what they leave out.
UNMEASURED = {
    "configs/.hermes/.hermes_history": "hermes_cli/profiles.py: the CLI's prompt history",
    "configs/.hermes/state-snapshots/": "hermes_cli/backup.py _QUICK_SNAPSHOTS_DIR: full state.db copies",
    "configs/.hermes/gateway.pid": "gateway/status.py _get_pid_path",
    "configs/.hermes/gateway.lock": "gateway/status.py _GATEWAY_LOCK_FILENAME",
    "configs/.hermes/processes.json": "tools/process_registry.py CHECKPOINT_PATH, recovered at gateway start",
    "configs/.hermes/cron.pid": "hermes_cli/backup.py _EXCLUDED_NAMES",
    "configs/.hermes/.restart_notify.json": "gateway/run_notifications.py _send_restart_notification",
    "configs/.hermes/.restart_pending.json": "gateway/run.py _planned_restart_notification_path",
    "configs/.hermes/kanban/": "hermes_cli/backup.py _QUICK_STATE_FILES kanban/boards",
    "configs/.hermes/pending_messages/": (
        "gateway/shutdown_flush.py _get_flush_dir: queued messages, the overflow FIFO and spooled "
        "transcript rows; recover_pending_spool replays them when the gateway starts"
    ),
    "configs/.hermes/gateway-starts.log": (
        "gateway/status.py record_start_and_check_storm: the respawn-storm ledger "
        "hermes_cli/gateway.py _respawn_storm_backoff reads at gateway start"
    ),
    "configs/.hermes/.update_*": (
        "gateway/run_notifications_update.py: .update_pending(.claimed).json, .update_output.txt, "
        ".update_exit_code, .update_prompt.json, .update_response; hermes_cli/profiles.py .update_check"
    ),
    "configs/.hermes/checkpoints/": "tools/checkpoint_manager.py CHECKPOINT_BASE; checkpoints.enabled is off",
    "configs/.hermes/moa-traces/": "agent/moa_trace.py, per-session MoA traces; moa.save_traces is off",
    "configs/.hermes/a2a_conversations/": "plugins/platforms/a2a/protocol.py, hermes' own A2A platform; not enabled",
}

LEAVE_OUT = [
    # the runtime's own
    "configs/.auth_token", "configs/.platform_inbound_secret",
    # conversation history and copies of it
    "configs/.hermes/state.db", "configs/.hermes/state.db-wal", "configs/.hermes/state.db-shm",
    "configs/.hermes/state.db.pre-update-emergency-20261101T040000Z.bak",
    "configs/.hermes/sessions/sessions.json", "configs/.hermes/sessions/20261112_093000_abc.jsonl",
    "configs/.hermes/response_store.db", "configs/.hermes/response_store.db-wal",
    "configs/.hermes/runs_idempotency.db", "configs/.hermes/runs_idempotency.db-wal",
    "configs/.hermes/.hermes_history",
    "configs/.hermes/pending_messages/pending-0f3c9a7e5b.json",
    "configs/.hermes/pending_messages/pending-77aa01.json.bad",
    "configs/.hermes/state-snapshots/20261101-040000/state.db",
    "configs/.hermes/backups/hermes-backup-20261101.zip",
    "configs/.hermes/cache/images/img_1.png", "configs/.hermes/cache/documents/report.pdf",
    "configs/.hermes/cache/models_dev.json",
    # logs
    "configs/.hermes/logs/agent.log", "configs/.hermes/logs/errors.log", "configs/.hermes/logs/gateway.log",
    "configs/.hermes/gateway.log", "configs/.hermes/molecule-mcp-server.log", "configs/.hermes/gateway-starts.log",
    # the agent's own gateway and its processes
    "configs/.hermes/gateway.pid", "configs/.hermes/gateway.lock", "configs/.hermes/gateway_state.json",
    "configs/.hermes/processes.json", "configs/.hermes/cron.pid",
    "configs/.hermes/.restart_notify.json", "configs/.hermes/.restart_pending.json",
    "configs/.hermes/.update_pending.json", "configs/.hermes/.update_pending.claimed.json",
    "configs/.hermes/.update_output.txt", "configs/.hermes/.update_exit_code",
    "configs/.hermes/.update_prompt.json", "configs/.hermes/.update_response", "configs/.hermes/.update_check",
    # work the gateway starts by itself
    "configs/.hermes/cron/jobs.json", "configs/.hermes/cron/output/a1b2/2026-11-12_09-00.md",
    "configs/.hermes/kanban.db", "configs/.hermes/kanban.db-shm", "configs/.hermes/kanban/boards/ops/kanban.db",
    # per-session history of features this image keeps off
    "configs/.hermes/checkpoints/store/HEAD", "configs/.hermes/checkpoints/indexes/9f2c.json",
    "configs/.hermes/moa-traces/20261112_093000_abc.jsonl",
    "configs/.hermes/a2a_conversations/peer-1.jsonl",
]

KEEP = [
    # identity, config and credentials
    "configs/.hermes/SOUL.md", "configs/.hermes/config.yaml", "configs/.hermes/.env", "configs/.hermes/auth.json",
    "configs/.hermes/mcp-tokens/linear.json", "configs/.hermes/pairing/molecule-a2a.json",
    # skills, memories, plugins
    "configs/.hermes/skills/ops/SKILL.md", "configs/.hermes/skills/.archive/old-report/SKILL.md",
    "configs/.hermes/skills/.curator_backups/2026-11-01T00-00-00Z/manifest.json",
    "configs/.hermes/.curator_backups/2026-11-01T00-00-00Z/manifest.json",
    "configs/.hermes/memories/MEMORY.md", "configs/.hermes/memories/USER.md", "configs/.hermes/memory_store.db",
    "configs/.hermes/plugins/molecule_turn_activity/__init__.py",
    "configs/.hermes/plugins/molecule_turn_activity/plugin.yaml",
    # small state a run may read
    "configs/.hermes/source-checks/f00d.json", "configs/.hermes/channel_directory.json",
    "configs/.hermes/.clean_shutdown", "configs/.hermes/installs",
    # the workspace's own files
    "configs/config.yaml", "configs/system-prompt.md", "configs/plugins/gmail/plugin.yaml",
    "configs/.molecule_plugins_boot_state.json", "configs/.config-relay-materialized",
    "rtstate/workspace/.molecule/chat-uploads/brief.pdf", "rtstate/workspace/.molecule/memory/goals.json",
    "rtstate/workspace/.molecule/idle-prompt", "rtstate/home/agent/.hermes/auth/token.json",
    "rtstate/home/agent/.molecule/plugin-state/state.json",
    # traps: names the patterns above must not reach outside their own place
    "configs/.hermes/skills/ops/sessions/notes.md", "configs/.hermes/skills/ops/logs/howto.md",
    "configs/.hermes/skills/x/gateway.log", "configs/.hermes/skills/cron/SKILL.md",
    "configs/.hermes/skills/kanban/SKILL.md", "configs/.hermes/memories/state.db-notes.md",
    "configs/.hermes/plugins/p/cache/data.json",
    "configs/.hermes/skills/ops/pending_messages/README.md", "configs/.hermes/skills/x/gateway-starts.log",
    "configs/.hermes/skills/ops/.update_pending.json", "configs/.hermes/.updated",
    "configs/.hermes/memories/checkpoints/plan.md", "configs/.hermes/skills/moa-traces/SKILL.md",
    "configs/.hermes/plugins/p/a2a_conversations/x.jsonl",
]


def _patterns() -> list[str]:
    return light_copy.load_patterns(str(EXCLUDE_FILE))


def _own_patterns() -> list[str]:
    return _patterns()[len(light_copy.BUILTIN_EXCLUDES):]


def _excluded(rel: str) -> bool:
    """True when the copy leaves ``rel`` out: it, or a directory above it, matches."""
    patterns = _patterns()
    parts = rel.split("/")
    for depth in range(1, len(parts) + 1):
        if light_copy.is_excluded("/".join(parts[:depth]), depth < len(parts), patterns):
            return True
    return False


def _logical_dockerfile() -> str:
    return re.sub(r"\\\s*\n\s*", " ", (ROOT / "Dockerfile").read_text(encoding="utf-8"))


def test_the_image_points_the_copy_step_at_the_list():
    df = _logical_dockerfile()
    assert light_copy.EXCLUDE_FILE_ENV == "MOLECULE_LIGHT_COPY_EXCLUDE_FILE"
    assert f"COPY light-copy-exclude.txt {IMAGE_PATH}" in df
    env_lines = [line for line in df.splitlines() if line.startswith("ENV ")]
    assert any(f"{light_copy.EXCLUDE_FILE_ENV}={IMAGE_PATH}" in line.split() for line in env_lines)
    # Editing the list must not rebuild the hermes install layers above it.
    assert df.index("ARG HERMES_COMMIT=") < df.index("COPY light-copy-exclude.txt")


def test_the_list_loads_and_stays_inside_hermes_home():
    own = _own_patterns()
    assert own, "the list is empty"
    assert all(p.startswith("configs/.hermes/") for p in own), own
    assert len(own) == len(set(own)) and not set(own) & set(light_copy.BUILTIN_EXCLUDES)


def test_every_line_is_on_a_real_claim_or_documented():
    def on_claim(pattern: str) -> bool:
        return any(light_copy.is_excluded(m.rstrip("/"), m.endswith("/"), [pattern]) for m in MEASURED)

    assert {p for p in _own_patterns() if not on_claim(p)} == set(UNMEASURED)


@pytest.mark.parametrize("rel", LEAVE_OUT)
def test_conversation_logs_process_state_and_queued_work_are_left_out(rel):
    assert _excluded(rel)


@pytest.mark.parametrize("rel", KEEP)
def test_identity_credentials_skills_memories_and_plugins_are_kept(rel):
    assert not _excluded(rel)


@pytest.mark.skipif(os.name != "posix", reason="the copy step is POSIX-only")
def test_a_real_copy_of_a_hermes_claim(tmp_path, monkeypatch, capsys):
    src, dst = tmp_path / "agent", tmp_path / "run"
    dst.mkdir()
    for rel in LEAVE_OUT + KEEP:
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel == "configs/.hermes/installs":
            path.symlink_to("/home/agent/.hermes/installs")
        else:
            path.write_text(f"{rel}\n", encoding="utf-8")
    monkeypatch.setenv(light_copy.EXCLUDE_FILE_ENV, str(EXCLUDE_FILE))
    monkeypatch.setenv("RUN_TOKEN", "run-token")

    assert light_copy.main(["--src", str(src), "--dst", str(dst), "--token-env", "RUN_TOKEN"]) == 0, capsys.readouterr()

    copied = {
        str(path.relative_to(dst)).replace(os.sep, "/")
        for path in dst.rglob("*")
        if path.is_symlink() or path.is_file()
    }
    assert copied == set(KEEP) | {"configs/.auth_token"}
    assert (dst / "configs/.auth_token").read_text(encoding="utf-8") == "run-token"
    assert os.readlink(dst / "configs/.hermes/installs") == "/home/agent/.hermes/installs"


def test_this_file_runs_in_the_required_gate():
    steps = yaml.safe_load((ROOT / ".gitea/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]["conformance"]["steps"]
    (script,) = [s.get("run", "") for s in steps if s.get("name") == "Run adapter and private-runtime contracts"]
    assert "tests/test_light_copy_exclude.py" in script.split()
