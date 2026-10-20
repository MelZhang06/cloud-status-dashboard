"""Exercise the real deploy script with stub CLIs; never contacts AWS or Docker."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_script(tmp_path, args=(), **overrides):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "commands.jsonl"
    stub = f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as f:
    f.write(json.dumps([name, *args]) + "\\n")
if name == "terraform":
    if args[:2] == ["state", "list"]:
        if os.environ.get("HAS_STATE") == "1": print("aws_ecr_repository.app")
    elif args[:2] == ["output", "-raw"]:
        print({"ecr_repository_url":"123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/cloud-status", "ecs_cluster_name":"demo-cluster", "ecs_service_name":"demo-service", "ecs_task_definition_arn":"arn:task:2", "alb_dns_name":"http://example.test"}[args[2]])
elif name == "aws":
    if args[:2] == ["sts", "get-caller-identity"]: print("123456789012")
    elif args[:2] == ["ecr", "get-login-password"]: print("test-password")
    elif args[:2] == ["ecs", "describe-services"]: print(os.environ.get("ACTUAL_TASK", "arn:task:2"))
elif name == "docker" and args[0] == "login":
    sys.stdin.read()
elif name == "curl":
    url = args[-1]
    if url.endswith("/health"): print('{"status":"healthy"}')
    elif url.endswith("/api/status"):
        print(json.dumps({"data_state": os.environ.get("DATA_STATE", "ok"), "last_success_at":"2026-10-20T00:00:00Z", "services":[{}]*10}))
    else: print("<html>Dashboard</html>")
'''
    for name in ("aws", "terraform", "docker", "curl"):
        path = bindir / name
        path.write_text(stub)
        path.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{Path(sys.executable).parent}:{os.environ['PATH']}", "COMMAND_LOG": str(log)}
    for key in ("EXPECTED_AWS_ACCOUNT_ID", "IMAGE_TAG", "ACTUAL_TASK", "HAS_STATE", "DATA_STATE"):
        env.pop(key, None)
    env.update(overrides)
    result = subprocess.run(["bash", str(ROOT / "deploy.sh"), *args], env=env, capture_output=True, text=True)
    commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, commands


def test_initial_deploy_pushes_image_before_full_apply_and_verifies(tmp_path):
    result, commands = run_script(tmp_path, IMAGE_TAG="test-build")
    assert result.returncode == 0, result.stderr
    assert "Deployment verified" in result.stdout
    bootstrap = commands.index(["terraform", "apply", "-target=aws_ecr_repository.app"])
    push = next(i for i,c in enumerate(commands) if c[:2] == ["docker", "push"])
    apply = commands.index(["terraform", "apply", "-var=image_tag=test-build"])
    wait = next(i for i,c in enumerate(commands) if c[:3] == ["aws", "ecs", "wait"])
    assert bootstrap < push < apply < wait
    assert any(c[:2] == ["docker", "build"] and "linux/amd64" in c for c in commands)
    assert ["terraform", "output", "-raw", "aws_region"] not in commands


def test_wrong_account_exits_before_terraform(tmp_path):
    result, commands = run_script(tmp_path, EXPECTED_AWS_ACCOUNT_ID="999999999999")
    assert result.returncode != 0
    assert "Wrong AWS account" in result.stderr
    assert not any(c[0] == "terraform" for c in commands)


def test_stable_service_with_rolled_back_task_is_failure(tmp_path):
    result, commands = run_script(tmp_path, ACTUAL_TASK="arn:task:1", HAS_STATE="1")
    assert result.returncode != 0
    assert "rolled back" in result.stderr
    assert not any(c[0] == "curl" for c in commands)
    assert not any("-target=aws_ecr_repository.app" in c for c in commands)


def test_skip_build_uses_existing_tag_and_needs_no_docker(tmp_path):
    result, commands = run_script(tmp_path, ["--skip-build"], IMAGE_TAG="previous-release", HAS_STATE="1")
    assert result.returncode == 0, result.stderr
    assert not any(c[0] == "docker" for c in commands)
    assert any(c[:3] == ["aws", "ecr", "describe-images"] for c in commands)
    assert ["terraform", "apply", "-var=image_tag=previous-release"] in commands


def test_feed_failure_is_not_reported_as_verified_deploy(tmp_path):
    result, _ = run_script(tmp_path, DATA_STATE="unavailable")
    assert result.returncode != 0
    assert "Deployment verified" not in result.stdout


def test_destroy_needs_no_docker_and_does_not_skip_confirmation(tmp_path):
    result, commands = run_script(tmp_path, ["destroy"])
    assert result.returncode == 0
    assert ["terraform", "destroy"] in commands
    assert not any(c[0] == "docker" or "-auto-approve" in c for c in commands)
