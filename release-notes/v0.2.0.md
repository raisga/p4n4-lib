First release of **p4n4-lib** on PyPI: the shared library behind the p4n4 CLI and p4n4-api.

```bash
pip install p4n4-lib==0.2.0    # Python 3.11+
```

There is no 0.1.0. The package was versioned 0.1.0 while it lived only on GitHub, and 0.2.0 ships together with `p4n4` (CLI) 0.2.0.

## What's in it

- **Projects:** `.p4n4.json` manifests, flat single-layer and per-layer multi-layer layouts, and validation that returns `(passed, errors)`, with no framework dependencies (only `pyyaml`).
- **Layers:** `iot`, `ai`, `edge` and `dashboard`, each with its repo, the files it copies, and its required files and env keys.
- **Scaffolding:** fetches a stack from a local checkout or a shallow clone and writes `.env` from the stack's `.env.example`, quoting values so Docker Compose reads `$`, `#`, spaces and quotes literally.
- **Compose project names** per layer (`layout.compose_project_name`, written by `scaffold_layer`), so projects on one host keep separate volumes.
- **Template projects** are validated against the template's own `.env.example`.
- **Docker Compose wrappers** (`up`, `down`, `ps`, `logs`), with a fallback to the standalone `docker-compose` when the v2 plugin is missing. `down` stops every profile, so no service is left running.
- **Secrets:** token generation, the rotatable-key policy, and external secrets (`MQTT_REMOTE_PASSWORD`) that are always masked and never rotated.
- **Windows:** stacks are cloned with LF line endings, and project files are read and written as UTF-8 with LF.

## Compatibility

- Used by **p4n4 (CLI) 0.2.0** and **p4n4-api 0.1.0**, which both require `p4n4-lib>=0.2.0`.

Full list: [CHANGELOG.md](https://github.com/raisga/p4n4-lib/blob/main/CHANGELOG.md).
