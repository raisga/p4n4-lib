"""Tests for Compose command detection and v1 fallback."""

from __future__ import annotations

import json
import subprocess

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
        if cmd[:2] == ["docker", "inspect"]:
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
                "Name": "/proj_mosquitto_1",
                "Config": {"Labels": {"com.docker.compose.service": "mosquitto"}},
                "State": {"Status": "running", "Health": {"Status": "healthy"}},
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
            "Name": "proj_mosquitto_1",
            "Service": "mosquitto",
            "State": "running",
            "Health": "healthy",
            "Publishers": [{"PublishedPort": 1883, "TargetPort": 1883, "Protocol": "tcp"}],
        }
    ]
