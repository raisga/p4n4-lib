"""Tests for Compose command detection and v1 fallback."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from p4n4_lib import compose


@pytest.fixture(autouse=True)
def _clear_cache():
    compose.compose_cmd.cache_clear()
    yield
    compose.compose_cmd.cache_clear()


def _fake_run(calls, plugin_ok, inspect_out=""):
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(cmd, 0 if plugin_ok else 1)
        if cmd[:3] == ["docker-compose", "ps", "-q"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n")
        if cmd[:3] == ["docker", "container", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=inspect_out)
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    return run


def _which(available):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def test_prefers_v2_plugin(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(compose.shutil, "which", _which({"docker", "docker-compose"}))
    monkeypatch.setattr(compose.subprocess, "run", _fake_run(calls, plugin_ok=True))
    compose.up(tmp_path)
    assert calls[-1] == ["docker", "compose", "up", "-d"]


def test_falls_back_to_standalone(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(compose.shutil, "which", _which({"docker", "docker-compose"}))
    monkeypatch.setattr(compose.subprocess, "run", _fake_run(calls, plugin_ok=False))
    compose.up(tmp_path, pull=True)
    assert calls[-2:] == [["docker-compose", "pull"], ["docker-compose", "up", "-d"]]


def test_down_enables_every_profile(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(compose.shutil, "which", _which({"docker", "docker-compose"}))
    monkeypatch.setattr(compose.subprocess, "run", _fake_run(calls, plugin_ok=True))
    compose.down(tmp_path, volumes=True)
    assert calls[-1] == ["docker", "compose", "--profile", "*", "down", "-v"]


def test_down_standalone_has_no_profile_flag(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(compose.shutil, "which", _which({"docker", "docker-compose"}))
    monkeypatch.setattr(compose.subprocess, "run", _fake_run(calls, plugin_ok=False))
    compose.down(tmp_path)
    assert calls[-1] == ["docker-compose", "down"]


def test_missing_compose_raises(monkeypatch):
    monkeypatch.setattr(compose.shutil, "which", _which({"docker"}))
    monkeypatch.setattr(compose.subprocess, "run", _fake_run([], plugin_ok=False))
    with pytest.raises(compose.ComposeNotFoundError):
        compose.compose_cmd()


def test_legacy_ps_normalizes_inspect(monkeypatch, tmp_path):
    inspect = json.dumps(
        [
            {
                "Id": "abc123",
                "Name": "/proj_mosquitto_1",
                "Config": {"Labels": {"com.docker.compose.service": "mosquitto"}},
                "State": {"Status": "running", "Health": {"Status": "healthy"}, "ExitCode": 0},
                "NetworkSettings": {
                    "Ports": {
                        "1883/tcp": [{"HostIp": "0.0.0.0", "HostPort": "1883"}],
                        "9001/tcp": None,
                    }
                },
            }
        ]
    )
    monkeypatch.setattr(compose.shutil, "which", _which({"docker-compose"}))
    monkeypatch.setattr(
        compose.subprocess, "run", _fake_run([], plugin_ok=False, inspect_out=inspect)
    )
    assert compose.ps(tmp_path) == [
        {
            "ID": "abc123",
            "Name": "proj_mosquitto_1",
            "Service": "mosquitto",
            "State": "running",
            "Health": "healthy",
            "ExitCode": 0,
            "Publishers": [{"PublishedPort": 1883, "TargetPort": 1883, "Protocol": "tcp"}],
        }
    ]


def test_ps_lists_stopped_containers(monkeypatch, tmp_path):
    calls = []
    exited = json.dumps({"Service": "influxdb", "State": "exited", "ExitCode": 1})

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0, stdout=exited + "\n")

    monkeypatch.setattr(compose.shutil, "which", _which({"docker"}))
    monkeypatch.setattr(compose.subprocess, "run", run)
    assert compose.ps(tmp_path) == [{"Service": "influxdb", "State": "exited", "ExitCode": 1}]
    assert calls[-1] == ["docker", "compose", "ps", "--all", "--format", "json"]


@pytest.mark.parametrize("plugin_ok", [True, False])
def test_ps_raises_when_compose_fails(monkeypatch, tmp_path, plugin_ok):
    # A daemon that's down must not look like a stack with nothing running
    def run(cmd, **kwargs):
        if cmd[:3] == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(cmd, 0 if plugin_ok else 1)
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="Cannot connect to the Docker daemon"
        )

    monkeypatch.setattr(compose.shutil, "which", _which({"docker", "docker-compose"}))
    monkeypatch.setattr(compose.subprocess, "run", run)
    with pytest.raises(compose.DockerError, match="Cannot connect to the Docker daemon"):
        compose.ps(tmp_path)


def _conflict_run(config_out, inspect_out, config_rc=0):
    def run(cmd, **kwargs):
        if cmd[:3] == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[-1] == "config":
            return subprocess.CompletedProcess(cmd, config_rc, stdout=config_out)
        if cmd[:3] == ["docker", "container", "inspect"]:
            # Missing names make inspect exit 1 while still printing the found ones
            return subprocess.CompletedProcess(cmd, 1, stdout=inspect_out)
        raise AssertionError(cmd)

    return run


def _container(name, project=None, working_dir=None, status="running"):
    labels = {}
    if project:
        labels["com.docker.compose.project"] = project
    if working_dir:
        labels["com.docker.compose.project.working_dir"] = working_dir
    return {"Name": f"/{name}", "State": {"Status": status}, "Config": {"Labels": labels}}


_CONFIG = """\
name: demo-iot
services:
  influxdb:
    container_name: p4n4-influxdb
  mqtt:
    container_name: p4n4-mqtt
  helper:
    image: busybox
"""


def test_name_conflicts_reports_other_projects(monkeypatch, tmp_path):
    inspect_out = json.dumps(
        [
            _container("p4n4-influxdb", "other-iot", "/projects/other/iot"),
            _container("p4n4-mqtt", status="exited"),
        ]
    )
    monkeypatch.setattr(compose.shutil, "which", _which({"docker"}))
    monkeypatch.setattr(compose.subprocess, "run", _conflict_run(_CONFIG, inspect_out))
    assert compose.name_conflicts(tmp_path) == [
        compose.NameConflict(
            "p4n4-influxdb", "running", "other-iot", Path("/projects/other/iot")
        ),
        compose.NameConflict("p4n4-mqtt", "exited", None, None),
    ]


def test_name_conflicts_ignores_own_containers(monkeypatch, tmp_path):
    inspect_out = json.dumps([_container("p4n4-influxdb", "demo-iot", str(tmp_path))])
    monkeypatch.setattr(compose.shutil, "which", _which({"docker"}))
    monkeypatch.setattr(compose.subprocess, "run", _conflict_run(_CONFIG, inspect_out))
    assert compose.name_conflicts(tmp_path) == []


def test_name_conflicts_v1_takes_project_name_from_env(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=demo-iot\n")
    config = _CONFIG.replace("name: demo-iot\n", "")
    inspect_out = json.dumps([_container("p4n4-influxdb", "demo-iot", str(tmp_path))])
    monkeypatch.setattr(compose.shutil, "which", _which({"docker", "docker-compose"}))

    run = _conflict_run(config, inspect_out)

    def v1_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(cmd, 1)
        return run(cmd, **kwargs)

    monkeypatch.setattr(compose.subprocess, "run", v1_run)
    assert compose.name_conflicts(tmp_path) == []


def test_name_conflicts_v1_project_name_keeps_dashes(monkeypatch, tmp_path):
    # docker-compose v1 keeps "-" and "_" when it derives the name from the directory
    stack = tmp_path / "Shop-Floor_iot"
    stack.mkdir()
    config = _CONFIG.replace("name: demo-iot\n", "")
    inspect_out = json.dumps([_container("p4n4-influxdb", "shop-floor_iot", str(stack))])
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    monkeypatch.setattr(compose.shutil, "which", _which({"docker-compose"}))
    monkeypatch.setattr(compose.subprocess, "run", _conflict_run(config, inspect_out))
    assert compose.name_conflicts(stack) == []


def test_name_conflicts_empty_when_config_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(compose.shutil, "which", _which({"docker"}))
    monkeypatch.setattr(compose.subprocess, "run", _conflict_run("", "", config_rc=1))
    assert compose.name_conflicts(tmp_path) == []


def test_name_conflicts_checks_every_stack_in_one_inspect(monkeypatch, tmp_path):
    iot, ai = tmp_path / "iot", tmp_path / "ai"
    iot.mkdir()
    ai.mkdir()
    configs = {
        iot: _CONFIG,
        ai: "name: demo-ai\nservices:\n  ollama:\n    container_name: p4n4-ollama\n",
    }
    inspected = []

    def run(cmd, cwd=None, **kwargs):
        if cmd[:3] == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[-1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout=configs[cwd])
        if cmd[:3] == ["docker", "container", "inspect"]:
            inspected.append(cmd[3:])
            held = [
                _container("p4n4-mqtt", "demo-iot", str(iot)),  # this project's own
                _container("p4n4-ollama", "other-ai", "/other/ai"),
            ]
            return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps(held))
        raise AssertionError(cmd)

    monkeypatch.setattr(compose.shutil, "which", _which({"docker"}))
    monkeypatch.setattr(compose.subprocess, "run", run)
    assert compose.name_conflicts(iot, ai) == [
        compose.NameConflict("p4n4-ollama", "running", "other-ai", Path("/other/ai"))
    ]
    assert [sorted(names) for names in inspected] == [["p4n4-influxdb", "p4n4-mqtt", "p4n4-ollama"]]


def _host_run(calls, containers, networks):
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["docker", "ps", "-a"]:
            ids = "\n".join(c["Name"] for c in containers)
            return subprocess.CompletedProcess(cmd, 0, stdout=ids)
        if cmd[:3] == ["docker", "container", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(containers))
        if cmd[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(networks))
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    return run


def _on(container, *networks):
    container["NetworkSettings"] = {"Networks": {n: {} for n in networks}}
    return container


def test_host_projects_stops_network_owners_last(monkeypatch):
    containers = [
        _on(_container("p4n4-mqtt", "a-iot", "/a/iot"), "p4n4-net"),
        _on(_container("p4n4-influxdb", "a-iot", "/a/iot", status="exited"), "p4n4-net"),
        _on(_container("p4n4-ollama", "b-ai", "/b/ai"), "p4n4-net"),
        _on(_container("p4n4-stray"), "bridge"),
    ]
    networks = [
        {"Name": "p4n4-net", "Labels": {"com.docker.compose.project": "a-iot"}},
        {"Name": "bridge", "Labels": {}},
    ]
    monkeypatch.setattr(compose.subprocess, "run", _host_run([], containers, networks))
    assert compose.host_projects() == [
        compose.HostProject(None, None, (("p4n4-stray", "running"),)),
        compose.HostProject("b-ai", Path("/b/ai"), (("p4n4-ollama", "running"),)),
        compose.HostProject(
            "a-iot",
            Path("/a/iot"),
            (("p4n4-influxdb", "exited"), ("p4n4-mqtt", "running")),
        ),
    ]


def test_host_projects_empty_without_containers(monkeypatch):
    calls = []
    monkeypatch.setattr(compose.subprocess, "run", _host_run(calls, [], []))
    assert compose.host_projects() == []
    assert [c[:3] for c in calls] == [["docker", "ps", "-a"]]


def test_host_projects_raises_when_docker_fails(monkeypatch):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="permission denied")

    monkeypatch.setattr(compose.subprocess, "run", run)
    with pytest.raises(compose.DockerError, match="permission denied"):
        compose.host_projects()


def test_down_project_by_name_outside_any_compose_file(monkeypatch, tmp_path):
    calls = []
    cwds = []

    def run(cmd, **kwargs):
        cwds.append(kwargs.get("cwd"))
        return _fake_run(calls, plugin_ok=True)(cmd, **kwargs)

    monkeypatch.setattr(compose.shutil, "which", _which({"docker"}))
    monkeypatch.setattr(compose.subprocess, "run", run)
    project = compose.HostProject("a-iot", tmp_path, (("p4n4-mqtt", "running"),))
    assert compose.down_project(project, volumes=True) == 0
    assert calls[-1] == ["docker", "compose", "-p", "a-iot", "down", "-v"]
    assert cwds[-1] != tmp_path
    assert not cwds[-1].exists()  # a temporary empty directory


def test_down_project_removes_containers_outside_compose(monkeypatch):
    calls = []
    monkeypatch.setattr(compose.subprocess, "run", _fake_run(calls, plugin_ok=True))
    project = compose.HostProject(None, None, (("p4n4-a", "running"), ("p4n4-b", "exited")))
    compose.down_project(project)
    assert calls == [["docker", "rm", "-f", "p4n4-a", "p4n4-b"]]


def test_down_project_v1_uses_the_project_files(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(compose.shutil, "which", _which({"docker", "docker-compose"}))
    monkeypatch.setattr(compose.subprocess, "run", _fake_run(calls, plugin_ok=False))
    compose.down_project(compose.HostProject("a-iot", tmp_path, (("p4n4-mqtt", "running"),)))
    assert calls[-1] == ["docker-compose", "down"]
    compose.down_project(compose.HostProject("a-iot", tmp_path / "gone", (("p4n4-x", "up"),)))
    assert calls[-1] == ["docker", "rm", "-f", "p4n4-x"]


def _network_run(calls, exists, create_rc=0, label="p4n4-net", attached=0):
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(
                cmd, 0 if exists else 1, stdout=f"{label} {attached}\n", stderr=""
            )
        if cmd[:3] == ["docker", "network", "rm"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, create_rc, stdout="", stderr="subnet overlaps")

    return run


def test_ensure_network_creates_it_with_compose_label(monkeypatch):
    calls = []
    monkeypatch.setattr(compose.subprocess, "run", _network_run(calls, exists=False))
    assert compose.ensure_network("p4n4-net", "172.20.0.0/16") is True
    create = calls[-1]
    assert create[:3] == ["docker", "network", "create"]
    assert create[-1] == "p4n4-net"
    assert ["--subnet", "172.20.0.0/16"] == create[create.index("--subnet") :][:2]
    # Without it, Compose refuses the network for p4n4-iot, which declares it itself
    assert "com.docker.compose.network=p4n4-net" in create


def test_ensure_network_leaves_an_existing_one(monkeypatch):
    calls = []
    monkeypatch.setattr(compose.subprocess, "run", _network_run(calls, exists=True))
    assert compose.ensure_network("p4n4-net", "172.20.0.0/16") is False
    assert [c[:3] for c in calls] == [["docker", "network", "inspect"]]


def test_ensure_network_raises_when_create_fails(monkeypatch):
    monkeypatch.setattr(compose.subprocess, "run", _network_run([], exists=False, create_rc=1))
    with pytest.raises(compose.DockerError, match="subnet overlaps"):
        compose.ensure_network("p4n4-net", "172.20.0.0/16")


def test_ensure_network_recreates_an_unlabelled_unused_one(monkeypatch):
    calls = []
    run = _network_run(calls, exists=True, label="<no value>", attached=0)
    monkeypatch.setattr(compose.subprocess, "run", run)
    assert compose.ensure_network("p4n4-net", "172.20.0.0/16") is True
    assert [c[:3] for c in calls] == [
        ["docker", "network", "inspect"],
        ["docker", "network", "rm"],
        ["docker", "network", "create"],
    ]
    assert "com.docker.compose.network=p4n4-net" in calls[-1]


def test_ensure_network_keeps_an_unlabelled_one_in_use(monkeypatch):
    # Removing it would cut the running stacks off from each other
    calls = []
    run = _network_run(calls, exists=True, label="<no value>", attached=3)
    monkeypatch.setattr(compose.subprocess, "run", run)
    assert compose.ensure_network("p4n4-net", "172.20.0.0/16") is False
    assert [c[:3] for c in calls] == [["docker", "network", "inspect"]]
