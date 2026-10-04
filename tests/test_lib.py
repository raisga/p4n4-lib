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


@pytest.mark.parametrize(
    "value",
    ["plainhex0123", "pa ss", "s3cr$t", "has#hash", "it's", "mix '\"$\\ all", ""],
)
def test_env_roundtrips_any_value(tmp_path, value):
    path = tmp_path / ".env"
    envutil.write(path, {"KEY": value})
    assert envutil.load(path) == {"KEY": value}


def test_env_quotes_only_when_needed(tmp_path):
    path = tmp_path / ".env"
    envutil.write(path, {"HEX": "abc123", "DOLLAR": "a$b", "QUOTE": "it's $x"})
    assert path.read_text().splitlines() == ["HEX=abc123", "DOLLAR='a$b'", 'QUOTE="it\'s \\$x"']


def test_env_load_strips_inline_comments_like_compose(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=value   # comment\nB=sensors/#\nC='kept # inside'\n")
    assert envutil.load(path) == {"A": "value", "B": "sensors/#", "C": "kept # inside"}


# ── secrets ───────────────────────────────────────────────────────────────────


def test_token_length_and_uniqueness():
    a, b = secretutil.token(16), secretutil.token(16)
    assert len(a) == 32
    assert a != b


def test_external_keys_are_never_rotated():
    assert not set(secretutil.EXTERNAL_KEYS) & set(secretutil.ROTATABLE_KEYS)
    assert "MQTT_REMOTE_PASSWORD" in secretutil.EXTERNAL_KEYS


def test_rotation_value_sizes():
    assert len(secretutil.rotation_value("GRAFANA_PASSWORD")) == 32
    assert len(secretutil.rotation_value("INFLUXDB_TOKEN")) == 64


# ── layers ────────────────────────────────────────────────────────────────────


def test_layer_registry_shape():
    assert set(LAYERS) == {"iot", "ai", "edge", "dashboard"}
    for layer in LAYERS.values():
        assert layer.repo_url.startswith("https://")
        assert layer.clone_prefix == f"p4n4-{layer.name}-"


# ── layout ────────────────────────────────────────────────────────────────────


def test_ordered_sorts_into_dependency_order():
    assert layout.ordered(["edge", "ai", "iot"]) == ["iot", "ai", "edge"]
    assert layout.ordered(["ai"]) == ["ai"]
    # The dashboard starts after the stacks it shows (and stops before them)
    assert layout.ordered(["dashboard", "iot", "ai"]) == ["iot", "ai", "dashboard"]


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


@pytest.mark.parametrize(
    ("project", "layers", "name", "expected"),
    [
        ("demo", ["iot"], "iot", "demo"),
        ("demo", ["iot", "ai"], "ai", "demo-ai"),
        ("My.Greenhouse 2", ["iot", "dashboard"], "dashboard", "my-greenhouse-2-dashboard"),
        ("_lab", ["iot"], "iot", "lab"),
        ("...", ["iot"], "iot", "p4n4"),
    ],
)
def test_compose_project_name(project, layers, name, expected):
    assert layout.compose_project_name(project, layers, name) == expected


def test_env_set_value_replaces_or_appends(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# COMPOSE_PROJECT_NAME=commented\nA=1\n")
    envutil.set_value(path, "COMPOSE_PROJECT_NAME", "demo-iot", comment="line one\nline two")
    assert path.read_text().endswith(
        "A=1\n\n# line one\n# line two\nCOMPOSE_PROJECT_NAME=demo-iot\n"
    )
    envutil.set_value(path, "A", "two words")
    assert envutil.load(path) == {"A": "two words", "COMPOSE_PROJECT_NAME": "demo-iot"}
    assert path.read_text().startswith("# COMPOSE_PROJECT_NAME=commented\n")


# ── scaffold ──────────────────────────────────────────────────────────────────


def test_fetch_source_local_path(tmp_path):
    src, tmpdir = fetch_source(LAYERS["iot"], tmp_path)
    assert src == tmp_path.resolve()
    assert tmpdir is None


def test_fetch_source_missing_path(tmp_path):
    with pytest.raises(ScaffoldError, match="does not exist"):
        fetch_source(LAYERS["iot"], tmp_path / "nope")


def test_fetch_source_clones_with_lf_line_endings(monkeypatch):
    import subprocess

    from p4n4_lib import scaffold

    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(scaffold.subprocess, "run", run)
    src, tmpdir = fetch_source(LAYERS["iot"])
    try:
        assert calls[0][:2] == ["git", "clone"]
        assert ["--config", "core.autocrlf=false"] == calls[0][4:6]
    finally:
        scaffold.shutil.rmtree(tmpdir, ignore_errors=True)


def test_env_and_manifest_written_as_utf8_lf(tmp_path):
    env_path = tmp_path / ".env"
    envutil.write(env_path, {"GRAFANA_PASSWORD": "pässwörd"})
    raw = env_path.read_bytes()
    assert b"\r\n" not in raw
    assert "pässwörd".encode() in raw
    assert envutil.load(env_path)["GRAFANA_PASSWORD"] == "pässwörd"

    path = tmp_path / mf.MANIFEST_FILE
    mf.save(path, mf.create("próject", ["iot"]))
    assert b"\r\n" not in path.read_bytes()
    assert mf.load(path)["project"] == "próject"


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


# ── template projects ─────────────────────────────────────────────────────────


def _make_template_project(tmp_path, **extra):
    data = {
        **mf.create("proj", ["iot"]),
        "template": {"name": "mqtt-influx-grafana", "version": "0.2.0"},
        **extra,
    }
    mf.save(tmp_path / mf.MANIFEST_FILE, data)
    (tmp_path / "docker-compose.yml").touch()
    (tmp_path / ".env.example").write_text("INFLUXDB_TOKEN=change-me\nCOMPOSE_PROFILES=demo\n")
    envutil.write(tmp_path / ".env", {"INFLUXDB_TOKEN": "x", "COMPOSE_PROFILES": ""})
    return data


def test_validate_template_project_skips_base_stack_files(tmp_path):
    # No Node-RED files or keys: the template replaces the base iot stack
    data = _make_template_project(tmp_path)
    passed, errors = validate_project(tmp_path, data)
    assert errors == []
    assert ".env: all required keys present" in passed


def test_validate_template_project_requires_env_example_keys(tmp_path):
    data = _make_template_project(tmp_path)
    envutil.write(tmp_path / ".env", {"COMPOSE_PROFILES": ""})
    _, errors = validate_project(tmp_path, data)
    assert errors == [".env missing required key: INFLUXDB_TOKEN"]


# ── dashboard block ───────────────────────────────────────────────────────────


def test_dashboard_block_is_optional():
    assert mf.dashboard_errors(mf.create("proj", ["iot"])) == []


def test_dashboard_block_valid(tmp_path):
    dashboard = {"grafana_path": "/d/p4n4-telemetry/telemetry", "tabs": ["services", "grafana"]}
    data = _make_template_project(tmp_path, dashboard=dashboard)
    passed, errors = validate_project(tmp_path, data)
    assert errors == []
    assert ".p4n4.json: dashboard settings valid" in passed


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ([], "dashboard must be an object"),
        ({"grafana_path": "d/x"}, 'grafana_path must be a path starting with "/"'),
        ({"tabs": []}, "tabs must be a non-empty list"),
        ({"tabs": ["grafana", "charts"]}, "unknown dashboard.tabs ['charts']"),
        ({"brand": "acme"}, "dashboard.brand is not a known setting"),
        ({"theme": "../shared/theme"}, "dashboard.theme must be a directory inside the project"),
        ({"theme": "/etc/theme"}, "dashboard.theme must be a directory inside the project"),
    ],
)
def test_dashboard_block_errors(block, message):
    errors = mf.dashboard_errors({"dashboard": block})
    assert len(errors) == 1 and message in errors[0]


def test_dashboard_theme_must_exist(tmp_path):
    data = _make_template_project(tmp_path, dashboard={"theme": "theme"})
    _, errors = validate_project(tmp_path, data)
    assert errors == ["Missing file: theme/brand.json (.p4n4.json dashboard.theme)"]

    (tmp_path / "theme").mkdir()
    (tmp_path / "theme" / "brand.json").write_text("{not json")
    _, errors = validate_project(tmp_path, data)
    assert len(errors) == 1 and "theme/brand.json is not valid JSON" in errors[0]

    (tmp_path / "theme" / "brand.json").write_text(
        json.dumps({"id": "verdant", "appName": "Verdant"})
    )
    passed, errors = validate_project(tmp_path, data)
    assert errors == []
    assert "theme/brand.json: dashboard theme" in passed


# ── dashboard layer ───────────────────────────────────────────────────────────


def test_dashboard_layer_copies_only_its_compose_file():
    layer = LAYERS["dashboard"]
    assert layer.copy_paths == ("docker-compose.yml",)
    assert layer.repo_url.endswith("/p4n4-dashboard.git")


def test_scaffold_sets_compose_project_name(tmp_path):
    from p4n4_lib.scaffold import scaffold_layer

    src = tmp_path / "src"
    src.mkdir()
    (src / "docker-compose.yml").write_text("services: {}\n")
    (src / ".env.example").write_text("DASHBOARD_PORT=8088\n")
    dest = tmp_path / "proj" / "dashboard"
    dest.mkdir(parents=True)
    scaffold_layer(dest, LAYERS["dashboard"], {}, source=src, compose_project_name="proj-dashboard")
    env = envutil.load(dest / ".env")
    assert env == {"DASHBOARD_PORT": "8088", "COMPOSE_PROJECT_NAME": "proj-dashboard"}

    # Without the argument, nothing is added (callers that manage names themselves)
    scaffold_layer(tmp_path, LAYERS["dashboard"], {}, source=src)
    assert "COMPOSE_PROJECT_NAME" not in envutil.load(tmp_path / ".env")


def test_dashboard_layer_scaffolds_and_validates(tmp_path):
    from p4n4_lib.scaffold import scaffold_layer

    src = tmp_path / "src"
    src.mkdir()
    (src / "docker-compose.yml").write_text("services: {}\n")
    (src / ".env.example").write_text("DASHBOARD_VERSION=1.1.0\nDASHBOARD_PORT=8088\nBRAND=p4n4\n")
    project = tmp_path / "proj"
    (project / "dashboard").mkdir(parents=True)
    scaffold_layer(project / "dashboard", LAYERS["dashboard"], {}, source=src)
    assert envutil.load(project / "dashboard" / ".env")["DASHBOARD_PORT"] == "8088"

    data = mf.create("proj", ["iot", "dashboard"])
    for rel in LAYERS["iot"].required_files:
        (project / "iot" / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / "iot" / rel).touch()
    envutil.write(project / "iot" / ".env", {k: "x" for k in LAYERS["iot"].required_env_keys})
    passed, errors = validate_project(project, data)
    assert errors == []
    assert "dashboard/.env: all required keys present" in passed
    assert layout.compose_dirs(project, data["layers"])[-1] == ("dashboard", project / "dashboard")
