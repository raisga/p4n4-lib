"""Tests for p4n4-lib shared modules."""

from __future__ import annotations

import json

import pytest

from p4n4_lib import env as envutil
from p4n4_lib import layout
from p4n4_lib import manifest as mf
from p4n4_lib import secrets as secretutil
from p4n4_lib.layers import DEFAULT_LAYERS, LAYERS, LEGACY_LAYERS
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
    assert not set(secretutil.EXTERNAL_KEYS) & set(secretutil.SECRET_KEYS)
    assert "MQTT_REMOTE_PASSWORD" in secretutil.EXTERNAL_KEYS


def test_setup_only_secrets_are_never_rotated():
    # Their services keep the first value: rotating .env alone breaks them
    assert not set(secretutil.SETUP_KEYS) & set(secretutil.ROTATABLE_KEYS)
    for key in ("INFLUXDB_TOKEN", "INFLUXDB_PASSWORD", "GRAFANA_PASSWORD", "N8N_ENCRYPTION_KEY"):
        assert key in secretutil.SETUP_KEYS
    assert set(secretutil.SECRET_KEYS) == {*secretutil.ROTATABLE_KEYS, *secretutil.SETUP_KEYS}


def test_profile_helpers():
    assert envutil.parse_profiles(" ollama, ,n8n ") == ["ollama", "n8n"]
    assert envutil.add_profile("ollama", "ei-runner") == "ollama,ei-runner"
    assert envutil.add_profile("ollama,ei-runner", "ei-runner") == "ollama,ei-runner"
    assert envutil.add_profile("", "ei-runner") == "ei-runner"
    assert envutil.remove_profile("ollama,ei-runner", "ei-runner") == "ollama"


def test_rotation_value_sizes():
    assert len(secretutil.rotation_value("GRAFANA_PASSWORD")) == 32
    assert len(secretutil.rotation_value("INFLUXDB_TOKEN")) == 64


# ── layers ────────────────────────────────────────────────────────────────────


def test_layer_registry_shape():
    assert set(LAYERS) == {"iot", "ai", "extras", "dashboard"}
    for layer in LAYERS.values():
        assert layer.repo_url.startswith("https://")
        assert layer.clone_prefix == f"p4n4-{layer.name}-"


def test_extras_is_opt_in_and_edge_is_legacy():
    # `--layer all` leaves the optional extras out
    assert DEFAULT_LAYERS == ("iot", "ai", "dashboard")
    assert LAYERS["extras"].optional
    assert LAYERS["extras"].repo_url.endswith("/p4n4-extras.git")
    # The edge layer's runner is the ai layer's ei-runner profile now
    assert LEGACY_LAYERS == {"edge": "ai"}
    assert {"runner", "edge-impulse", "onnx"} <= set(LAYERS["ai"].copy_paths)
    assert "runner/runner.py" in LAYERS["ai"].profile_files["ei-runner"]


# ── layout ────────────────────────────────────────────────────────────────────


def test_ordered_sorts_into_dependency_order():
    assert layout.ordered(["extras", "ai", "iot"]) == ["iot", "ai", "extras"]
    assert layout.ordered(["ai"]) == ["ai"]
    # The dashboard starts after the stacks it shows (and stops before them)
    assert layout.ordered(["dashboard", "iot", "ai"]) == ["iot", "ai", "dashboard"]
    # Unknown names are dropped
    assert layout.ordered(["iot", "nope"]) == ["iot"]


def test_ordered_keeps_legacy_layers_after_their_replacement():
    # Projects made before the edge layer merged into ai keep running it
    assert layout.ordered(["edge", "dashboard", "extras", "ai", "iot"]) == [
        "iot",
        "ai",
        "edge",
        "extras",
        "dashboard",
    ]
    assert layout.legacy(["iot", "ai", "edge"]) == ["edge"]
    assert layout.legacy(["iot", "ai"]) == []


def _ai_env(tmp_path, layers, profiles):
    ai = layout.layer_dir(tmp_path, layers, "ai")
    ai.mkdir(exist_ok=True)
    envutil.write(ai / ".env", {"COMPOSE_PROFILES": profiles})


def test_runner_layer(tmp_path):
    assert layout.runner_layer(tmp_path, ["iot"]) is None
    assert layout.runner_layer(tmp_path, ["iot", "edge"]) == "edge"
    _ai_env(tmp_path, ["iot", "ai"], "ollama")
    assert layout.runner_layer(tmp_path, ["iot", "ai"]) is None
    _ai_env(tmp_path, ["iot", "ai"], "ollama, ei-runner")
    assert layout.runner_layer(tmp_path, ["iot", "ai"]) == "ai"
    # Without a .env, nothing is enabled
    assert layout.runner_layer(tmp_path / "missing", ["iot", "ai"]) is None


def test_network_subnet(tmp_path):
    layers = ["iot", "ai"]
    # Without a .env, or with P4N4_SUBNET unset or empty, the default
    assert layout.network_subnet(tmp_path, layers) == layout.NETWORK_SUBNET
    (tmp_path / "ai").mkdir()
    (tmp_path / "ai" / ".env").write_text("P4N4_SUBNET=172.29.0.0/16\n")
    assert layout.network_subnet(tmp_path, layers) == "172.29.0.0/16"
    # iot's comes first: p4n4-iot creates the network with it
    (tmp_path / "iot").mkdir()
    (tmp_path / "iot" / ".env").write_text("P4N4_SUBNET=\n")
    assert layout.network_subnet(tmp_path, layers) == "172.29.0.0/16"
    (tmp_path / "iot" / ".env").write_text("P4N4_SUBNET=172.28.0.0/16\n")
    assert layout.network_subnet(tmp_path, layers) == "172.28.0.0/16"
    # A single-layer project keeps its .env at the root
    (tmp_path / ".env").write_text("P4N4_SUBNET=10.40.0.0/16\n")
    assert layout.network_subnet(tmp_path, ["iot"]) == "10.40.0.0/16"


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
    assert layout.compose_dirs(tmp_path, ["ai", "iot", "extras"]) == [
        ("iot", tmp_path / "iot"),
        ("ai", tmp_path / "ai"),
    ]


def test_compose_dirs_finds_a_legacy_edge_layer(tmp_path):
    for name in ("iot", "ai", "edge"):
        (tmp_path / name).mkdir()
        (tmp_path / name / layout.COMPOSE_FILE).touch()
    assert [name for name, _ in layout.compose_dirs(tmp_path, ["edge", "ai", "iot"])] == [
        "iot",
        "ai",
        "edge",
    ]


def test_compose_dirs_skips_unscaffolded_layers(tmp_path):
    (tmp_path / "iot").mkdir()
    (tmp_path / "iot" / layout.COMPOSE_FILE).touch()
    assert layout.compose_dirs(tmp_path, ["iot", "extras"]) == [("iot", tmp_path / "iot")]


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


def _make_ai_project(tmp_path, profiles):
    layer = LAYERS["ai"]
    data = mf.create("proj", ["ai"])
    mf.save(tmp_path / mf.MANIFEST_FILE, data)
    for rel in layer.required_files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    envutil.write(
        tmp_path / ".env",
        {"COMPOSE_PROFILES": profiles, **{key: "x" for key in layer.required_env_keys}},
    )
    return data


def test_validate_checks_runner_files_only_when_its_profile_is_enabled(tmp_path):
    # An ai layer from before the merge, without the runner: still valid
    data = _make_ai_project(tmp_path, "ollama")
    assert validate_project(tmp_path, data)[1] == []

    envutil.set_value(tmp_path / ".env", "COMPOSE_PROFILES", "ollama,ei-runner")
    _, errors = validate_project(tmp_path, data)
    assert "Missing file: runner/runner.py" in errors
    assert "Missing file: edge-impulse/models" in errors
    assert ".env missing required key: MODEL_BACKEND" in errors


def test_validate_skips_a_legacy_layer(tmp_path):
    data = _make_iot_project(tmp_path)
    data["layers"] = ["iot", "edge"]
    (tmp_path / "iot").mkdir()
    for rel in [*LAYERS["iot"].required_files]:
        path = tmp_path / "iot" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    envutil.write(tmp_path / "iot" / ".env", {k: "x" for k in LAYERS["iot"].required_env_keys})
    _, errors = validate_project(tmp_path, data)
    # The CLI warns about the edge layer; validation doesn't fail on it
    assert not [e for e in errors if "edge" in e]


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
    dashboard = {
        "grafana_path": "/d/p4n4-telemetry/telemetry",
        "tabs": ["services", "grafana"],
        "home": "docs/home.md",
    }
    data = _make_template_project(tmp_path, dashboard=dashboard)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "home.md").write_text("# Hello\n")
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
        ({"home": "../README.md"}, "dashboard.home must be a file inside the project"),
        ({"home": "/etc/motd"}, "dashboard.home must be a file inside the project"),
        ({"home": " "}, "dashboard.home must be a file inside the project"),
        ({"cameras": []}, "dashboard.cameras must be a non-empty list"),
        ({"cameras": ["floor"]}, "dashboard.cameras[0] must be an object"),
        (
            {"cameras": [{"id": "Floor 1", "name": "F", "port": 1984}]},
            "cameras[0].id must be lowercase",
        ),
        (
            {"cameras": [{"id": "a", "name": "A", "port": 1}, {"id": "a", "name": "B", "port": 2}]},
            "'a' is used twice",
        ),
        (
            {"cameras": [{"id": "a", "name": " ", "port": 1984}]},
            "cameras[0].name must be a non-empty string",
        ),
        ({"cameras": [{"id": "a", "name": "A"}]}, "needs one of url, port"),
        (
            {"cameras": [{"id": "a", "name": "A", "url": "http://cam", "port": 1}]},
            "needs one of url, port",
        ),
        (
            {"cameras": [{"id": "a", "name": "A", "device": "back", "port": 1}]},
            "needs one of url, port",
        ),
        (
            {"cameras": [{"id": "a", "name": "A", "device": "side"}]},
            'device must be "back" or "front"',
        ),
        (
            {"cameras": [{"id": "a", "name": "A", "device": "back", "path": "/s"}]},
            "path only goes with port",
        ),
        (
            {"cameras": [{"id": "a", "name": "A", "url": "rtsp://cam/s"}]},
            "url must be an absolute http(s) URL",
        ),
        (
            {"cameras": [{"id": "a", "name": "A", "url": "http://cam", "path": "/s"}]},
            "path only goes with port",
        ),
        ({"cameras": [{"id": "a", "name": "A", "port": 70000}]}, "port must be a TCP port"),
        ({"cameras": [{"id": "a", "name": "A", "port": True}]}, "port must be a TCP port"),
        (
            {"cameras": [{"id": "a", "name": "A", "port": 1984, "path": "stream"}]},
            'path must start with "/"',
        ),
        (
            {"cameras": [{"id": "a", "name": "A", "port": 1984, "fps": 5}]},
            "cameras[0].fps is not a known setting",
        ),
    ],
)
def test_dashboard_block_errors(block, message):
    errors = mf.dashboard_errors({"dashboard": block})
    assert len(errors) == 1 and message in errors[0]


@pytest.mark.parametrize(
    ("feed", "message"),
    [
        ("http://x/feed", "dashboard.feed must be an object"),
        ({}, "dashboard.feed.url must be an absolute http(s) URL"),
        ({"url": "/api/v1/feed"}, "dashboard.feed.url must be an absolute http(s) URL"),
        ({"url": "http://x/feed", "title": "Feed"}, "feed.title is not a known setting"),
        ({"url": "http://x/feed", "token": "secret"}, 'feed.token must be {"layer", "env"}'),
        ({"url": "http://x/feed", "token": {"layer": "iot", "env": "a-b"}}, "token.env must be"),
        ({"url": "http://x/feed", "token": {"layer": " ", "env": "TOKEN"}}, "token.layer must be"),
    ],
)
def test_dashboard_feed_errors(feed, message):
    errors = mf.dashboard_errors({"dashboard": {"feed": feed}})
    assert len(errors) == 1 and message in errors[0]


@pytest.mark.parametrize(
    ("chats", "message"),
    [
        ("http://x/chats", "dashboard.chats must be an object"),
        ({}, "dashboard.chats.url must be an absolute http(s) URL"),
        ({"url": "localhost:8096/chats"}, "dashboard.chats.url must be an absolute http(s) URL"),
        ({"url": "http://x/chats", "bot": "@b"}, "dashboard.chats.bot is not a known setting"),
        ({"url": "http://x/chats", "token": "t"}, 'dashboard.chats.token must be {"layer", "env"}'),
        (
            {"url": "http://x/chats", "token": {"layer": "ai", "env": "lower"}},
            "dashboard.chats.token.env must be an environment variable name",
        ),
    ],
)
def test_dashboard_chats_errors(chats, message):
    errors = mf.dashboard_errors({"dashboard": {"chats": chats}})
    assert len(errors) == 1 and message in errors[0]


def test_dashboard_chats_valid():
    chats = {
        "url": "http://localhost:8096/api/v1/chats",
        "token": {"layer": "ai", "env": "CHANNELS_ADMIN_TOKEN"},
    }
    assert mf.dashboard_errors({"dashboard": {"chats": chats}}) == []


def test_dashboard_feed_valid():
    feed = {
        "url": "http://localhost:8090/api/v1/feed",
        "token": {"layer": "iot", "env": "INGEST_READ_TOKEN"},
    }
    assert mf.dashboard_errors({"dashboard": {"tabs": ["feed"], "feed": feed}}) == []
    assert mf.dashboard_errors({"dashboard": {"feed": {"url": "https://x.example/feed"}}}) == []


@pytest.mark.parametrize(
    ("vision", "message"),
    [
        ("vision.json", "dashboard.vision must be an object"),
        ({}, "dashboard.vision.file must be a file inside the project"),
        ({"file": "../vision.json"}, "dashboard.vision.file must be a file inside the project"),
        ({"file": "v.json", "fps": 5}, "vision.fps is not a known setting"),
        ({"file": "v.json", "live": "ws://x/stream"}, "dashboard.vision.live must be an object"),
        ({"file": "v.json", "live": {}}, "live needs one of url or port"),
        (
            {"file": "v.json", "live": {"url": "ws://x/s", "port": 8000}},
            "live needs one of url or port",
        ),
        (
            {"file": "v.json", "live": {"url": "http://x/stream"}},
            "live.url must be an absolute ws(s) URL",
        ),
        (
            {"file": "v.json", "live": {"url": "ws://x/s", "path": "/s"}},
            "live.path only goes with port",
        ),
        ({"file": "v.json", "live": {"port": 0}}, "live.port must be a TCP port"),
        (
            {"file": "v.json", "live": {"port": 8000, "path": "stream"}},
            'live.path must start with "/"',
        ),
        (
            {"file": "v.json", "live": {"port": 8000, "token": "x"}},
            "live.token is not a known setting",
        ),
    ],
)
def test_dashboard_vision_errors(vision, message):
    errors = mf.dashboard_errors({"dashboard": {"vision": vision}})
    assert len(errors) == 1 and message in errors[0]


def test_dashboard_vision_valid():
    for vision in [
        {"file": "vision/boutique.json"},
        {"file": "v.json", "live": {"port": 8000, "path": "/stream"}},
        {"file": "v.json", "live": {"port": 8000}},
        {"file": "v.json", "live": {"url": "wss://edge.example/stream"}},
    ]:
        assert mf.dashboard_errors({"dashboard": {"tabs": ["video"], "vision": vision}}) == []


def test_dashboard_vision_file_is_checked(tmp_path):
    data = _make_template_project(tmp_path, dashboard={"vision": {"file": "vision.json"}})
    assert validate_project(tmp_path, data)[1] == [
        "Missing file: vision.json (.p4n4.json dashboard.vision.file)"
    ]
    (tmp_path / "vision.json").write_text("{not json")
    assert validate_project(tmp_path, data)[1][0].startswith("vision.json: not valid JSON")
    (tmp_path / "vision.json").write_text("[]")
    assert validate_project(tmp_path, data)[1] == [
        'vision.json: a vision overlay is an object with "cameras"'
    ]
    (tmp_path / "vision.json").write_text('{"cameras": {"a": {}, "b": {}}}')
    passed, errors = validate_project(tmp_path, data)
    assert errors == []
    assert "vision.json: vision overlay (2 cameras)" in passed


def test_dashboard_cameras_valid():
    cameras = [
        {"id": "floor", "name": "Sales floor", "port": 1984, "path": "/api/stream.mjpeg?src=floor"},
        {"id": "door", "name": "Door", "url": "https://cams.example.com/door.mjpeg"},
        {"id": "root", "name": "Root", "port": 8081},
        {"id": "dashcam", "name": "Dash cam", "device": "back"},
        {"id": "pilot", "name": "Pilot cam", "device": "front"},
    ]
    assert mf.dashboard_errors({"dashboard": {"tabs": ["video"], "cameras": cameras}}) == []


def test_dashboard_home_must_exist_when_named(tmp_path):
    data = _make_template_project(tmp_path, dashboard={"home": "docs/home.md"})
    passed, errors = validate_project(tmp_path, data)
    assert errors == ["Missing file: docs/home.md (.p4n4.json dashboard.home)"]

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "home.md").write_text("# Hello\n")
    passed, errors = validate_project(tmp_path, data)
    assert errors == []
    assert "docs/home.md: dashboard home page" in passed


def test_dashboard_default_home_is_optional(tmp_path):
    data = _make_template_project(tmp_path, dashboard={"tabs": ["services"]})
    _, errors = validate_project(tmp_path, data)
    assert errors == []


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


# ── project .gitignore ────────────────────────────────────────────────────────


def _git_ignored(repo, paths):
    """The paths git ignores in repo, by `git check-ignore`."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "--no-index", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    return set(result.stdout.split())


@pytest.mark.parametrize("prefix", ["", "iot/", "ai/"])
def test_project_gitignore_keeps_secrets_and_models_out(tmp_path, prefix):
    from p4n4_lib import scaffold

    assert scaffold.write_gitignore(tmp_path)
    secret = [
        f"{prefix}.env",
        f"{prefix}docker-compose.override.yml",
        f"{prefix}config/mosquitto/passwd",
        f"{prefix}config/mosquitto/certs/ca.crt",
        f"{prefix}config/node-red/flows/flows_cred.json",
        f"{prefix}edge-impulse/models/model.eim",
        f"{prefix}onnx/models/model.onnx",
        ".p4n4-api/jwt.key",
        "edge.pre-migrate/.env",
    ]
    kept = [
        ".p4n4.json",
        f"{prefix}.env.example",
        f"{prefix}docker-compose.yml",
        f"{prefix}config/mosquitto/mosquitto.conf",
        f"{prefix}config/mosquitto/certs/.gitkeep",
        f"{prefix}config/node-red/flows/flows.json",
        f"{prefix}edge-impulse/models/.gitkeep",
        f"{prefix}runner/runner.py",
    ]
    ignored = _git_ignored(tmp_path, secret + kept)
    assert ignored == set(secret)


def test_project_gitignore_leaves_an_existing_one(tmp_path):
    from p4n4_lib import scaffold

    (tmp_path / ".gitignore").write_text("mine\n")
    assert not scaffold.write_gitignore(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == "mine\n"
