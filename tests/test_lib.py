"""Tests for p4n4-lib shared modules."""

from __future__ import annotations

import json

import pytest

from p4n4_lib import env as envutil
from p4n4_lib import layout
from p4n4_lib import manifest as mf
from p4n4_lib import secrets as secretutil
from p4n4_lib.layers import LAYERS
from p4n4_lib.scaffold import ScaffoldError, fetch_source
from p4n4_lib.validate import validate_project

# ── manifest ──────────────────────────────────────────────────────────────────


def test_manifest_create_roundtrip(tmp_path):
    data = mf.create("proj", ["iot", "ai"])
    path = tmp_path / mf.MANIFEST_FILE
    mf.save(path, data)
    loaded = mf.load(path)
    assert loaded == data
    assert loaded["schema_version"] == mf.SCHEMA_VERSION
    assert loaded["project"] == "proj"
    assert loaded["layers"] == ["iot", "ai"]


def test_manifest_find_walks_up(tmp_path):
    (tmp_path / mf.MANIFEST_FILE).write_text("{}")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    found = mf.find(nested)
    assert found == tmp_path / mf.MANIFEST_FILE


def test_manifest_find_returns_none_when_absent(tmp_path):
    assert mf.find(tmp_path) is None


# ── env ───────────────────────────────────────────────────────────────────────


def test_env_load_skips_comments_and_blanks(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comment\n\nFOO=bar\nBAZ = qux \n")
    assert envutil.load(path) == {"FOO": "bar", "BAZ": "qux"}


def test_env_write_plain(tmp_path):
    path = tmp_path / ".env"
    envutil.write(path, {"FOO": "bar", "BAZ": "qux"})
    assert envutil.load(path) == {"FOO": "bar", "BAZ": "qux"}


def test_env_write_preserves_template_structure(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text("# Section\nFOO=default\nKEEP=asis\n")
    path = tmp_path / ".env"
    envutil.write(path, {"FOO": "override"}, template_path=template)
    text = path.read_text()
    assert "# Section" in text
    assert envutil.load(path) == {"FOO": "override", "KEEP": "asis"}


# ── secrets ───────────────────────────────────────────────────────────────────


def test_token_length_and_uniqueness():
    a, b = secretutil.token(16), secretutil.token(16)
    assert len(a) == 32
    assert a != b


def test_rotation_value_sizes():
    assert len(secretutil.rotation_value("GRAFANA_PASSWORD")) == 32
    assert len(secretutil.rotation_value("INFLUXDB_TOKEN")) == 64


# ── layers ────────────────────────────────────────────────────────────────────


def test_layer_registry_shape():
    assert set(LAYERS) == {"iot", "ai", "edge"}
    for layer in LAYERS.values():
        assert layer.repo_url.startswith("https://")
        assert layer.clone_prefix == f"p4n4-{layer.name}-"


# ── layout ────────────────────────────────────────────────────────────────────


def test_ordered_sorts_into_dependency_order():
    assert layout.ordered(["edge", "ai", "iot"]) == ["iot", "ai", "edge"]
    assert layout.ordered(["ai"]) == ["ai"]


def test_layer_dir_single_layer_is_project_root(tmp_path):
    assert layout.layer_dir(tmp_path, ["iot"], "iot") == tmp_path


def test_layer_dir_multi_layer_is_subdirectory(tmp_path):
    assert layout.layer_dir(tmp_path, ["iot", "ai"], "ai") == tmp_path / "ai"


def test_compose_dirs_single_layer_root(tmp_path):
    (tmp_path / layout.COMPOSE_FILE).touch()
    assert layout.compose_dirs(tmp_path, ["iot"]) == [("iot", tmp_path)]


def test_compose_dirs_multi_layer_subdirs(tmp_path):
    for name in ("iot", "ai"):
        (tmp_path / name).mkdir()
        (tmp_path / name / layout.COMPOSE_FILE).touch()
    assert layout.compose_dirs(tmp_path, ["ai", "iot", "edge"]) == [
        ("iot", tmp_path / "iot"),
        ("ai", tmp_path / "ai"),
    ]


def test_compose_dirs_skips_unscaffolded_layers(tmp_path):
    (tmp_path / "iot").mkdir()
    (tmp_path / "iot" / layout.COMPOSE_FILE).touch()
    assert layout.compose_dirs(tmp_path, ["iot", "edge"]) == [("iot", tmp_path / "iot")]


# ── scaffold ──────────────────────────────────────────────────────────────────


def test_fetch_source_local_path(tmp_path):
    src, tmpdir = fetch_source(LAYERS["iot"], tmp_path)
    assert src == tmp_path.resolve()
    assert tmpdir is None


def test_fetch_source_missing_path(tmp_path):
    with pytest.raises(ScaffoldError, match="does not exist"):
        fetch_source(LAYERS["iot"], tmp_path / "nope")


# ── validate ──────────────────────────────────────────────────────────────────


def _make_iot_project(tmp_path):
    layer = LAYERS["iot"]
    data = mf.create("proj", ["iot"])
    mf.save(tmp_path / mf.MANIFEST_FILE, data)
    for rel in layer.required_files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    envutil.write(tmp_path / ".env", {key: "x" for key in layer.required_env_keys})
    return data


def test_validate_passes_on_complete_project(tmp_path):
    data = _make_iot_project(tmp_path)
    passed, errors = validate_project(tmp_path, data)
    assert errors == []
    assert ".p4n4.json: valid" in passed
    assert ".env: all required keys present" in passed


def test_validate_flags_missing_file_and_key(tmp_path):
    data = _make_iot_project(tmp_path)
    (tmp_path / "config/mosquitto/mosquitto.conf").unlink()
    env = envutil.load(tmp_path / ".env")
    del env["GRAFANA_PASSWORD"]
    envutil.write(tmp_path / ".env", env)
    _, errors = validate_project(tmp_path, data)
    assert "Missing file: config/mosquitto/mosquitto.conf" in errors
    assert ".env missing required key: GRAFANA_PASSWORD" in errors


def test_validate_flags_schema_mismatch(tmp_path):
    data = _make_iot_project(tmp_path)
    data["schema_version"] = 99
    _, errors = validate_project(tmp_path, data)
    assert any("schema_version mismatch" in e for e in errors)


def test_validate_flags_missing_env_file(tmp_path):
    data = _make_iot_project(tmp_path)
    (tmp_path / ".env").unlink()
    _, errors = validate_project(tmp_path, data)
    assert ".env file not found" in errors
    assert "Missing file: .env" in errors


def test_validate_manifest_json_is_valid_json(tmp_path):
    data = _make_iot_project(tmp_path)
    raw = (tmp_path / mf.MANIFEST_FILE).read_text()
    assert json.loads(raw) == data


def _make_multi_project(tmp_path):
    layer_names = ["iot", "ai"]
    data = mf.create("proj", layer_names)
    mf.save(tmp_path / mf.MANIFEST_FILE, data)
    for name in layer_names:
        layer = LAYERS[name]
        base = tmp_path / name
        for rel in layer.required_files:
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        envutil.write(base / ".env", {key: "x" for key in layer.required_env_keys})
    return data


def test_validate_multi_layer_passes_and_prefixes_labels(tmp_path):
    data = _make_multi_project(tmp_path)
    passed, errors = validate_project(tmp_path, data)
    assert errors == []
    assert "iot/.env: all required keys present" in passed
    assert "ai/.env: all required keys present" in passed
    assert "iot/docker-compose.yml" in passed
    assert "ai/config/letta/letta.conf" in passed


def test_validate_multi_layer_flags_per_layer_errors(tmp_path):
    data = _make_multi_project(tmp_path)
    (tmp_path / "ai" / "config/letta/letta.conf").unlink()
    env = envutil.load(tmp_path / "iot" / ".env")
    del env["GRAFANA_PASSWORD"]
    envutil.write(tmp_path / "iot" / ".env", env)
    _, errors = validate_project(tmp_path, data)
    assert "Missing file: ai/config/letta/letta.conf" in errors
    assert "iot/.env missing required key: GRAFANA_PASSWORD" in errors
