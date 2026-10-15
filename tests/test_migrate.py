"""Tests for p4n4_lib.migrate (`p4n4 migrate edge`)."""

from __future__ import annotations

import pytest
import yaml

from p4n4_lib import env as envutil
from p4n4_lib import layout, migrate
from p4n4_lib import manifest as mf
from p4n4_lib.validate import validate_project

# A project's edge layer as p4n4-edge scaffolded it
EDGE_COMPOSE = """\
services:

  # Edge Impulse Inference Runner
  ei-runner:
    build: ./runner
    container_name: p4n4-ei-runner
    profiles: [ei-runner]
    restart: ${RESTART_POLICY:-unless-stopped}
    ports:
      - "${EI_RUNNER_BIND:-127.0.0.1}:8080:8080"
    environment:
      MODEL_BACKEND: ${MODEL_BACKEND:-auto}
      MQTT_TOPIC_RESULTS: ${MQTT_TOPIC_RESULTS:-inference/{device}/result}
      INFLUXDB_URL: http://p4n4-influxdb:8086
    volumes:
      - ./edge-impulse/models:/models:ro
      - ./onnx/models:/onnx-models:ro
      - ei-runner-data:/data
    healthcheck:
      test: ["CMD-SHELL", "wget -qO /dev/null http://localhost:8080/health || exit 1"]
    networks:
      - p4n4-net

networks:
  p4n4-net:
    external: true
    name: p4n4-net

volumes:
  ei-runner-data:
"""

# A project's ai layer from before the merge, as p4n4-ai scaffolded it
AI_COMPOSE = """\
# Every service is optional
services:

  # Local LLM Runtime (Ollama)
  ollama:
    image: ollama/ollama:0.35.1
    container_name: p4n4-ollama
    profiles: [ollama]
    volumes:
      - ollama-data:/root/.ollama
    networks:
      - p4n4-net

# NETWORKS
networks:
  p4n4-net:
    external: true
    name: p4n4-net

# VOLUMES
volumes:
  ollama-data:
"""

# The merged p4n4-ai stack: its services plus the runner's
_RUNNER_SERVICE = EDGE_COMPOSE.split("services:\n", 1)[1].split("networks:", 1)[0]
MERGED_AI_COMPOSE = (
    AI_COMPOSE.replace("services:\n", "services:\n" + _RUNNER_SERVICE, 1) + "  ei-runner-data:\n"
)

# Its .env.example: the ai layer's required keys and the runner's
AI_ENV_EXAMPLE = """\
COMPOSE_PROFILES=ollama
LETTA_SERVER_PASSWORD=lettapassword
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=adminpassword
N8N_ENCRYPTION_KEY=change-me-32-char-encryption-key
N8N_HOST=localhost
MODEL_BACKEND=auto
MQTT_HOST=p4n4-mqtt
MQTT_PORT=1883
INFLUXDB_TOKEN=p4n4-stack-token
INFLUXDB_ORG=ming
INFLUXDB_BUCKET=raw_telemetry
INFLUXDB_BUCKET_AI_EVENTS=ai_events
TZ=UTC
"""

# A template's own edge layer (video), not the runner
VIDEO_COMPOSE = """\
services:
  video:
    image: alexxit/go2rtc:1.9.14
    container_name: p4n4-video
    volumes:
      - uplink-state:/state
    networks:
      - p4n4-net
networks:
  p4n4-net:
    external: true
volumes:
  uplink-state:
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _edge_layer(base, compose=EDGE_COMPOSE, env=None):
    _write(base / layout.COMPOSE_FILE, compose)
    envutil.write(
        base / ".env",
        env
        if env is not None
        else {
            "COMPOSE_PROFILES": "ei-runner",
            "COMPOSE_PROJECT_NAME": "proj-edge",
            "MODEL_BACKEND": "onnx",
            "MQTT_HOST": "p4n4-mqtt",
            "INFLUXDB_TOKEN": "tok",
            "INFLUXDB_BUCKET_AI_EVENTS": "ai_events",
            "TZ": "Europe/Madrid",
        },
    )
    _write(base / "runner" / "runner.py", "# the project's runner\n")
    _write(base / "runner" / "Dockerfile", "FROM python:3.11-slim\n")
    _write(base / "edge-impulse" / "models" / "my.eim", "eim")
    _write(base / "onnx" / "models" / ".gitkeep", "")


def _project(tmp_path, layers):
    project = tmp_path / "proj"
    project.mkdir()
    mf.save(project / mf.MANIFEST_FILE, mf.create("proj", layers))
    return project


def _ai_source(tmp_path):
    """A p4n4-ai checkout: the merged stack, with ei-runner."""
    src = tmp_path / "p4n4-ai"
    _write(src / layout.COMPOSE_FILE, MERGED_AI_COMPOSE)
    _write(src / ".env.example", AI_ENV_EXAMPLE)
    _write(src / "config" / "letta" / "letta.conf", "")
    _write(src / "scripts" / "pull-models.sh", "")
    _write(src / "runner" / "runner.py", "# the stack's runner\n")
    _write(src / "runner" / "Dockerfile", "FROM python:3.11-slim\n")
    _write(src / "edge-impulse" / "models" / ".gitkeep", "")
    _write(src / "onnx" / "models" / ".gitkeep", "")
    return src


def test_target_of():
    assert migrate.target_of(yaml.safe_load(EDGE_COMPOSE)) == "ai"
    assert migrate.target_of(yaml.safe_load(VIDEO_COMPOSE)) == "extras"


def test_migrates_into_an_existing_ai_layer(tmp_path):
    project = _project(tmp_path, ["iot", "ai", "edge"])
    _write(project / "ai" / layout.COMPOSE_FILE, AI_COMPOSE)
    envutil.write(
        project / "ai" / ".env",
        {"COMPOSE_PROFILES": "ollama,n8n", "INFLUXDB_TOKEN": "tok", "TZ": "UTC"},
    )
    _edge_layer(project / "edge")

    result = migrate.migrate_edge(project)

    assert result.target == "ai"
    assert result.layers == ["iot", "ai"]
    assert mf.load(project / mf.MANIFEST_FILE)["layers"] == ["iot", "ai"]
    # The edge layer is kept aside, not deleted
    assert not (project / "edge").exists()
    assert (project / "edge.pre-migrate" / layout.COMPOSE_FILE).exists()

    # The service joins the ai compose file; its comments stay
    ai_compose = (project / "ai" / layout.COMPOSE_FILE).read_text()
    assert "# Local LLM Runtime (Ollama)" in ai_compose
    compose = yaml.safe_load(ai_compose)
    assert set(compose["services"]) == {"ollama", "ei-runner"}
    assert set(compose["volumes"]) == {"ollama-data", "ei-runner-data"}
    runner = compose["services"]["ei-runner"]
    assert runner["build"] == "./runner"
    assert runner["environment"]["MQTT_TOPIC_RESULTS"] == (
        "${MQTT_TOPIC_RESULTS:-inference/{device}/result}"
    )
    assert result.services_added == ["ei-runner"]

    # The runner and its models move next to it
    assert (project / "ai" / "runner" / "runner.py").read_text() == "# the project's runner\n"
    assert (project / "ai" / "edge-impulse" / "models" / "my.eim").exists()

    # .env: new keys appended, shared keys kept, the profile enabled
    env = envutil.load(project / "ai" / ".env")
    assert env["MODEL_BACKEND"] == "onnx"
    assert env["COMPOSE_PROFILES"] == "ollama,n8n,ei-runner"
    assert env["TZ"] == "UTC"
    assert result.env_conflicts == {"TZ": ("UTC", "Europe/Madrid")}
    assert "COMPOSE_PROJECT_NAME" not in env
    assert result.profile_added


def test_respects_a_runner_that_was_turned_off(tmp_path):
    project = _project(tmp_path, ["iot", "ai", "edge"])
    _write(project / "ai" / layout.COMPOSE_FILE, AI_COMPOSE)
    envutil.write(project / "ai" / ".env", {"COMPOSE_PROFILES": "ollama"})
    _edge_layer(project / "edge", env={"COMPOSE_PROFILES": "", "MODEL_BACKEND": "auto"})

    result = migrate.migrate_edge(project)

    assert not result.profile_added
    assert envutil.load(project / "ai" / ".env")["COMPOSE_PROFILES"] == "ollama"


def test_keeps_models_already_in_the_ai_layer(tmp_path):
    project = _project(tmp_path, ["iot", "ai", "edge"])
    _write(project / "ai" / layout.COMPOSE_FILE, AI_COMPOSE)
    envutil.write(project / "ai" / ".env", {"COMPOSE_PROFILES": "ollama"})
    _write(project / "ai" / "runner" / "runner.py", "# ai's runner\n")
    _write(project / "ai" / "edge-impulse" / "models" / "my.eim", "newer")
    _edge_layer(project / "edge")

    result = migrate.migrate_edge(project)

    assert (project / "ai" / "runner" / "runner.py").read_text() == "# ai's runner\n"
    assert (project / "ai" / "edge-impulse" / "models" / "my.eim").read_text() == "newer"
    assert "runner/" not in result.files_copied


def test_scaffolds_an_ai_layer_when_the_project_has_none(tmp_path):
    project = _project(tmp_path, ["iot", "edge"])
    _edge_layer(project / "edge")

    result = migrate.migrate_edge(
        project, source_ai=_ai_source(tmp_path), new_env={"LETTA_SERVER_PASSWORD": "generated"}
    )

    assert result.scaffolded
    assert result.layers == ["iot", "ai"]
    env = envutil.load(project / "ai" / ".env")
    assert env["COMPOSE_PROFILES"] == "ei-runner"
    assert env["COMPOSE_PROJECT_NAME"] == "proj-ai"
    assert env["LETTA_SERVER_PASSWORD"] == "generated"
    assert env["INFLUXDB_TOKEN"] == "tok"
    # The stack's runner, the project's models
    assert (project / "ai" / "runner" / "runner.py").read_text() == "# the stack's runner\n"
    assert (project / "ai" / "edge-impulse" / "models" / "my.eim").exists()
    assert (project / "edge.pre-migrate" / "runner" / "runner.py").exists()


def test_migrates_a_flat_edge_only_project_in_place(tmp_path):
    project = _project(tmp_path, ["edge"])
    _edge_layer(project)

    result = migrate.migrate_edge(project, source_ai=_ai_source(tmp_path))

    assert result.layers == ["ai"]
    assert result.target_dir == project
    env = envutil.load(project / ".env")
    assert env["COMPOSE_PROFILES"] == "ei-runner"
    # Single-layer projects use the project's name, as before: same volumes
    assert env["COMPOSE_PROJECT_NAME"] == "proj"
    assert "ei-runner" in yaml.safe_load((project / layout.COMPOSE_FILE).read_text())["services"]
    assert (project / "edge-impulse" / "models" / "my.eim").exists()
    assert (project / "edge.pre-migrate" / layout.COMPOSE_FILE).exists()


def test_migrated_project_validates(tmp_path):
    project = _project(tmp_path, ["iot", "edge"])
    _edge_layer(project / "edge")
    migrate.migrate_edge(project, source_ai=_ai_source(tmp_path))
    # The iot layer isn't scaffolded in this test: only the ai layer's checks count
    errors = validate_project(project, mf.load(project / mf.MANIFEST_FILE))[1]
    assert [e for e in errors if "ai/" in e] == []


def test_a_project_left_with_one_layer_is_flattened(tmp_path):
    project = _project(tmp_path, ["ai", "edge"])
    _write(project / "ai" / layout.COMPOSE_FILE, AI_COMPOSE)
    envutil.write(
        project / "ai" / ".env", {"COMPOSE_PROFILES": "ollama", "COMPOSE_PROJECT_NAME": "proj-ai"}
    )
    _edge_layer(project / "edge")

    result = migrate.migrate_edge(project)

    # A single-layer project keeps its stack at the root (layout.layer_dir)
    assert result.layers == ["ai"]
    assert result.target_dir == project
    assert not (project / "ai").exists()
    assert layout.compose_dirs(project, ["ai"]) == [("ai", project)]
    assert "ei-runner" in yaml.safe_load((project / layout.COMPOSE_FILE).read_text())["services"]
    # The Compose project name stays, so the ai containers keep their volumes
    assert envutil.load(project / ".env")["COMPOSE_PROJECT_NAME"] == "proj-ai"
    assert (project / "runner" / "runner.py").exists()


def test_user_keys_the_stack_does_not_list_come_along(tmp_path):
    project = _project(tmp_path, ["iot", "edge"])
    _edge_layer(project / "edge")
    envutil.set_value(project / "edge" / ".env", "MY_SETTING", "kept")

    result = migrate.migrate_edge(project, source_ai=_ai_source(tmp_path))

    assert envutil.load(project / "ai" / ".env")["MY_SETTING"] == "kept"
    assert "MY_SETTING" in result.env_added


def test_a_template_edge_layer_becomes_extras_and_keeps_its_volumes(tmp_path):
    project = _project(tmp_path, ["iot", "ai", "edge"])
    _edge_layer(project / "edge", compose=VIDEO_COMPOSE, env={"CAMERA_URL": ""})

    result = migrate.migrate_edge(project)

    assert result.target == "extras"
    assert result.layers == ["iot", "ai", "extras"]
    assert result.backup is None
    assert (project / "extras" / layout.COMPOSE_FILE).read_text() == VIDEO_COMPOSE
    # Compose named its volumes after the edge directory: keep that project name
    assert envutil.load(project / "extras" / ".env")["COMPOSE_PROJECT_NAME"] == "edge"
    assert result.compose_project_name == "edge"


def test_refuses_without_an_edge_layer(tmp_path):
    project = _project(tmp_path, ["iot", "ai"])
    with pytest.raises(migrate.MigrateError, match="no edge layer"):
        migrate.migrate_edge(project)


def test_refuses_when_a_backup_exists(tmp_path):
    project = _project(tmp_path, ["ai", "edge"])
    _write(project / "ai" / layout.COMPOSE_FILE, AI_COMPOSE)
    _edge_layer(project / "edge")
    (project / "edge.pre-migrate").mkdir()
    with pytest.raises(migrate.MigrateError, match="already exists"):
        migrate.migrate_edge(project)
    assert (project / "edge" / layout.COMPOSE_FILE).exists()


def test_failure_restores_the_edge_layer(tmp_path):
    project = _project(tmp_path, ["ai", "edge"])
    # An ai compose file without the network the runner joins
    _write(project / "ai" / layout.COMPOSE_FILE, "services:\n  ollama:\n    image: x:1\n")
    _edge_layer(project / "edge")

    with pytest.raises(migrate.MigrateError, match="no network p4n4-net"):
        migrate.migrate_edge(project)

    assert (project / "edge" / layout.COMPOSE_FILE).read_text() == EDGE_COMPOSE
    assert not (project / "edge.pre-migrate").exists()
    assert mf.load(project / mf.MANIFEST_FILE)["layers"] == ["ai", "edge"]


def test_spliced_compose_file_is_valid_for_compose(tmp_path):
    """The spliced file loads in Docker Compose, when it's installed."""
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    project = _project(tmp_path, ["iot", "ai", "edge"])
    _write(project / "ai" / layout.COMPOSE_FILE, AI_COMPOSE)
    envutil.write(project / "ai" / ".env", {"COMPOSE_PROFILES": "ollama"})
    _edge_layer(project / "edge")
    migrate.migrate_edge(project)
    result = subprocess.run(
        ["docker", "compose", "--profile", "*", "config", "--services"],
        cwd=project / "ai",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and "Cannot connect" in result.stderr:
        pytest.skip("docker unavailable")
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.split()) == {"ollama", "ei-runner"}
