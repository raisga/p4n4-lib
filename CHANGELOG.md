# Changelog

All notable changes to `p4n4-lib` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.2.0] - 2026-10-03

First release on PyPI. There is no 0.1.0: the package was versioned 0.1.0 while it lived
only on GitHub, and the first published version is 0.2.0, released together with
`p4n4` (CLI) 0.2.0.

### Added

- Shared library extracted from `p4n4-cli`, used by `p4n4-cli` and `p4n4-api`:
  - `manifest`: find (walk-up), load, save and create `.p4n4.json`
  - `env`: dotenv read/write that keeps the `.env.example` template's comments and layout
  - `layers`: layer registry (repo URLs, copy paths, required files and env keys)
  - `layout`: flat single-layer projects, per-layer subdirectories for multi-layer projects
  - `scaffold`: fetch stack sources (local path or shallow clone) and copy them into a project
  - `validate`: pure project checks returning `(passed, errors)`
  - `secrets`: token generation and the rotatable-key policy
  - `compose`: Docker Compose wrappers (`up`, `down`, `ps`, `logs`)
- `edge` layer: compose file, runner, model directories and `.env`.
- `dashboard` layer (p4n4-dashboard web service), and checks for the optional `.p4n4.json`
  `dashboard` block (`grafana_path`, `tabs`, `theme`, and a `brand.json` in a named theme).
- Template projects (a `template` block in `.p4n4.json`) are validated against the
  template's own `.env.example` instead of the base stack's file list.
- `layout.compose_project_name()`: a Compose project name per layer (`<project>` or
  `<project>-<layer>`, sanitized), and `scaffold_layer(..., compose_project_name=...)`, which writes
  it to the layer's `.env` as `COMPOSE_PROJECT_NAME`. Without it, every multi-layer project on a
  host shared the `iot`, `ai`, … Compose projects and so their volumes.
- `env.set_value()`: set one key in an existing `.env` (replace, or append with a comment).
- `MQTT_REMOTE_PASSWORD` is an external secret: shown fully masked, never rotated.
- The IoT layer requires `NODE_RED_USER` and `NODE_RED_PASSWORD`; `NODE_RED_PASSWORD` is
  rotatable. Node-RED flows are copied from `config/node-red/flows/`.

### Changed

- `compose` falls back to the standalone `docker-compose` (v1) when the Docker Compose v2
  plugin is missing.
- `compose.down` enables every profile (`--profile "*"`), so services started by name, or
  dropped from `COMPOSE_PROFILES` since, are stopped too. Not available on v1.

### Fixed

- `.env` values containing `$`, `#`, spaces or quotes are quoted, so Docker Compose reads
  them literally.
- Windows: stacks are cloned with `core.autocrlf=false`, so scaffolded shell scripts keep LF
  line endings and run inside the Linux containers. `.env` and `.p4n4.json` are read and
  written as UTF-8 with LF line endings instead of the platform defaults.

[Unreleased]: https://github.com/raisga/p4n4-lib/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/raisga/p4n4-lib/releases/tag/v0.2.0
