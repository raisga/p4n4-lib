# p4n4-lib

Shared Python library mediating between P4N4 stacks and clients
(`p4n4-cli`, `p4n4-api`, `p4n4-dashboard`).

Framework-free by design: no CLI/TUI/web dependencies, only `pyyaml`.
Presentation (prompts, tables, colors) stays in the clients.

## Modules

| Module | Purpose |
|--------|---------|
| `p4n4_lib.manifest` | `.p4n4.json` manifest: find (walk-up), load, save, create |
| `p4n4_lib.env` | Dotenv read/write, template-preserving writes |
| `p4n4_lib.layers` | Layer registry: repo URLs, copy paths, required files/env keys per layer |
| `p4n4_lib.layout` | Project layout: flat for single-layer projects, per-layer subdirectories for multi-layer |
| `p4n4_lib.scaffold` | Fetch stack sources (local path or shallow clone) and copy into a project |
| `p4n4_lib.validate` | Pure project validation returning (passed, errors) check lists |
| `p4n4_lib.secrets` | Token generation and the rotatable-key policy |
| `p4n4_lib.compose` | Docker Compose subprocess wrappers (`up`, `down`, `ps`, `logs`) |

Stack repo URLs live in `p4n4_lib/sources.yaml`.

## Install

```bash
pip install p4n4-lib                      # once published
pip install -e path/to/core/lib           # monorepo development
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## License

MIT
