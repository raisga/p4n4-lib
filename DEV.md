# p4n4-lib — Developer Guide

> **Status:** Draft v0.1 — 2026-03-19
> **Scope:** Rust core library with PyO3 Python bindings; shared by `p4n4-api` and `p4n4-cli`

---

## Table of Contents

1. [Purpose & Goals](#1-purpose--goals)
2. [Architecture Overview](#2-architecture-overview)
3. [Repository Layout](#3-repository-layout)
4. [Module Reference](#4-module-reference)
5. [Feature Flags](#5-feature-flags)
6. [Consuming the Library](#6-consuming-the-library)
7. [Python Bindings (PyO3 / maturin)](#7-python-bindings-pyo3--maturin)
8. [Error Handling](#8-error-handling)
9. [Build & Development Setup](#9-build--development-setup)
10. [Testing Strategy](#10-testing-strategy)
11. [Versioning & Compatibility](#11-versioning--compatibility)
12. [CI/CD](#12-cicd)

---

## 1. Purpose & Goals

`p4n4-lib` is the shared core library for the P4N4 platform. It provides a
single, well-tested implementation of the data models, service clients, auth
primitives, and project manifest handling that both `p4n4-api` (Rust) and
`p4n4-cli` (Python) depend on.

### Goals

- **Single source of truth** — models, client logic, and auth live here; consumers do not re-implement them.
- **Dual-language** — compiled as a native Rust `rlib` for the API and as a PyO3 extension module (`cdylib`) for the CLI.
- **Feature-gated** — consumers compile only what they need (e.g. the CLI does not pull in async HTTP runtime).
- **No I/O in the core** — models and manifest are pure data; clients are feature-gated and injected at the consumer level.
- **Tested independently** — the library has its own unit and integration test suite, not delegated to consumers.

### Non-Goals

- Not a binary crate — it does not run standalone.
- Not a protocol gateway — routing, middleware, and HTTP handling belong in `p4n4-api`.
- Not a CLI framework — command parsing and UX belong in `p4n4-cli`.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                        consumers                        │
│                                                         │
│   ┌──────────────────┐         ┌──────────────────┐     │
│   │    p4n4-api       │         │    p4n4-cli       │     │
│   │    (Rust binary)  │         │  (Python package) │     │
│   └────────┬─────────┘         └────────┬──────────┘     │
│            │ Cargo.toml dep             │ pip install     │
│            │ features = ["full"]        │ p4n4lib wheel   │
└────────────┼────────────────────────────┼────────────────┘
             │                            │
             ▼                            ▼
┌────────────────────────────────────────────────────────────┐
│                        p4n4-lib                            │
│                                                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │  models  │  │ manifest │  │ secrets  │  │   auth   │  │
│  │(no deps) │  │(serde)   │  │(rand)    │  │(jwt/argon│  │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │                    clients (async)                   │  │
│  │  InfluxDB · MQTT · Ollama · Letta · Edge Runner      │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │         python.rs  (PyO3 extension module)           │  │
│  │  re-exports models, manifest, secrets, auth          │  │
│  └──────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
             │                            │
      rlib (static)                cdylib (.so/.pyd)
      → linked into               → imported by Python
        p4n4-api binary             as p4n4lib._p4n4lib
```

The crate declares **both** `cdylib` and `rlib` output types so that a single
`cargo build` produces both artefacts. `maturin` wraps the `cdylib` into a
wheel; the API adds `p4n4-lib` as a path or registry dependency.

---

## 3. Repository Layout

```
p4n4-lib/
├── Cargo.toml              ← crate manifest + feature definitions
├── Cargo.lock              ← committed (keep deterministic builds)
├── pyproject.toml          ← maturin build configuration
├── DEV.md                  ← this file
├── README.md               ← crate overview (user-facing)
├── CHANGELOG.md
├── LICENSE
│
├── src/
│   ├── lib.rs              ← public re-exports; feature-gated module declarations
│   ├── error.rs            ← P4n4Error + P4n4Result<T>
│   │
│   ├── models/
│   │   ├── mod.rs
│   │   ├── device.rs       ← Device, NewDevice, DeviceStatus
│   │   ├── telemetry.rs    ← TelemetryPoint, TelemetryQuery, TelemetryResponse
│   │   ├── inference.rs    ← InferenceRequest, InferenceResult
│   │   ├── agent.rs        ← ChatMessage, ChatRequest, ChatResponse
│   │   ├── health.rs       ← StackHealth, ServiceHealth
│   │   └── common.rs       ← PaginatedResponse<T>, ErrorBody
│   │
│   ├── manifest/
│   │   └── mod.rs          ← P4n4Manifest (read/write/validate .p4n4.json)
│   │
│   ├── secrets.rs          ← SecretGenerator: API keys, passwords, tokens
│   │
│   ├── auth/
│   │   └── mod.rs          ← Claims, Role, JwtCodec, ApiKeyHasher
│   │
│   ├── clients/
│   │   ├── mod.rs
│   │   ├── influxdb.rs     ← InfluxClient: write (line protocol) + query (Flux)
│   │   ├── mqtt.rs         ← MqttClient: publish + subscribe
│   │   ├── ollama.rs       ← OllamaClient: generate + chat
│   │   ├── letta.rs        ← LettaClient: agents list + send_message
│   │   └── edge.rs         ← EdgeClient: run_inference
│   │
│   └── python.rs           ← #[pymodule] — exposes subset to Python via PyO3
│
├── python/
│   └── p4n4lib/
│       ├── __init__.py     ← re-exports from the native extension + typed stubs
│       └── py.typed        ← PEP 561 marker
│
└── tests/
    ├── test_secrets.rs
    ├── test_manifest.rs
    ├── test_auth.rs
    └── test_clients.rs     ← integration tests (requires running services)
```

---

## 4. Module Reference

### 4.1 `models`

Pure data types shared across the entire platform. No async, no I/O.
All types derive `serde::Serialize + serde::Deserialize` and `Clone`.

| Type | Description |
|------|-------------|
| `Device` | Full device record (id, name, type, site, tags, topic_prefix, status, timestamps) |
| `NewDevice` | Create-request payload (no id, no timestamps) |
| `DeviceStatus` | `Active \| Inactive \| Deregistered` |
| `TelemetryPoint` | Single reading: device_id, measurement, value, optional timestamp |
| `TelemetryQuery` | Query parameters: device_id, measurement, from, to, limit |
| `TelemetryResponse` | Paginated list of `TelemetryPoint` |
| `InferenceRequest` | device_id + float values array |
| `InferenceResult` | label, confidence, anomaly_score, latency_ms, mode, timestamp |
| `ChatMessage` | role (`user \| assistant \| system`) + content |
| `ChatRequest` | agent_id + message string |
| `ChatResponse` | agent_id, response, memory_updated, latency_ms |
| `StackHealth` | Map of stack name → `ServiceHealth` map |
| `ServiceHealth` | status (`healthy \| degraded \| unreachable`) + optional latency_ms |
| `PaginatedResponse<T>` | Generic: data vec + pagination metadata |

### 4.2 `manifest`

Handles reading, writing, and validating `.p4n4.json` project manifest files.

```rust
pub struct P4n4Manifest {
    pub schema_version: u32,   // must be 1
    pub project: String,
    pub stacks: Vec<String>,   // "iot" | "ai" | "edge"
    pub created_at: DateTime<Utc>,
}

impl P4n4Manifest {
    pub fn load(path: &Path) -> P4n4Result<Self>;
    pub fn save(&self, path: &Path) -> P4n4Result<()>;
    pub fn validate(&self) -> P4n4Result<()>;
}
```

Used by `p4n4-cli` to read and write `.p4n4.json`, and by `p4n4-api` to
inspect a project manifest when the `manifest` feature is enabled.

### 4.3 `secrets`

Cryptographically secure secret generation. No async, no I/O. Backed by
`rand::thread_rng` (OS entropy via `getrandom`).

```rust
pub struct SecretGenerator;

impl SecretGenerator {
    /// p4n4_<40 base62 chars> — device API keys
    pub fn api_key() -> String;

    /// 32 random bytes, hex-encoded — JWT secrets, encryption keys
    pub fn hex_secret() -> String;

    /// 16 random bytes, base64url-encoded — short passwords
    pub fn password() -> String;

    /// UUIDv4 string
    pub fn uuid() -> String;
}
```

Used by:
- `p4n4-api`: generate API keys on device registration, JWT secret from env
- `p4n4-cli`: generate secrets during `p4n4 init` and `p4n4 secret`

### 4.4 `auth`

JWT encoding/decoding and API key hashing. Requires the `auth` feature.

```rust
pub enum Role { Device, Operator, Admin }

pub struct Claims {
    pub sub: String,   // "device:<id>" or "user:<name>"
    pub role: Role,
    pub iat: i64,
    pub exp: i64,
}

pub struct JwtCodec {
    secret: String,
    expiry_secs: u64,
    refresh_expiry_secs: u64,
}

impl JwtCodec {
    pub fn new(secret: &str, expiry_secs: u64, refresh_expiry_secs: u64) -> Self;
    pub fn encode(&self, sub: &str, role: Role) -> P4n4Result<String>;
    pub fn encode_refresh(&self, sub: &str) -> P4n4Result<String>;
    pub fn decode(&self, token: &str) -> P4n4Result<Claims>;
}

pub struct ApiKeyHasher;

impl ApiKeyHasher {
    /// Argon2id hash of the raw key — store this, not the key
    pub fn hash(raw_key: &str) -> P4n4Result<String>;
    /// Constant-time verification
    pub fn verify(raw_key: &str, hash: &str) -> P4n4Result<bool>;
}
```

Used exclusively by `p4n4-api`. Not exposed to the Python bindings.

### 4.5 `clients`

Async HTTP and MQTT clients for all platform services. Requires the `clients`
feature (pulls in `reqwest`, `rumqttc`, `tokio`).

All clients are constructed from a config struct and hold an internal
connection pool or persistent connection.

#### `InfluxClient`

```rust
pub struct InfluxConfig {
    pub url: String,
    pub token: String,
    pub org: String,
    pub bucket: String,
}

impl InfluxClient {
    pub async fn write(&self, points: &[TelemetryPoint]) -> P4n4Result<()>;
    pub async fn query(&self, q: &TelemetryQuery) -> P4n4Result<Vec<TelemetryPoint>>;
    pub async fn ping(&self) -> P4n4Result<u64>; // latency ms
}
```

`write` converts `TelemetryPoint` structs to InfluxDB line protocol and POSTs
to `/api/v2/write`. `query` builds a Flux query string from `TelemetryQuery`
fields and parses the CSV response.

#### `MqttClient`

```rust
pub struct MqttConfig {
    pub host: String,
    pub port: u16,
    pub client_id: String,
    pub username: Option<String>,
    pub password: Option<String>,
}

impl MqttClient {
    pub async fn connect(config: MqttConfig) -> P4n4Result<Self>;
    pub async fn publish(&self, topic: &str, payload: &[u8], qos: u8, retain: bool) -> P4n4Result<()>;
    pub async fn subscribe(&self, topic: &str) -> P4n4Result<MqttStream>;
    pub async fn ping(&self) -> P4n4Result<u64>;
}
```

`MqttStream` is a `Stream<Item = MqttMessage>` that can be polled by the API
for its SSE broadcast task.

#### `OllamaClient`

```rust
impl OllamaClient {
    pub async fn generate(&self, model: &str, prompt: &str) -> P4n4Result<String>;
    pub async fn chat(&self, model: &str, messages: &[ChatMessage]) -> P4n4Result<String>;
    pub async fn list_models(&self) -> P4n4Result<Vec<String>>;
    pub async fn ping(&self) -> P4n4Result<u64>;
}
```

Targets Ollama's OpenAI-compatible endpoint (`/api/generate`, `/api/chat`).

#### `LettaClient`

```rust
impl LettaClient {
    pub async fn list_agents(&self) -> P4n4Result<Vec<String>>;
    pub async fn send_message(&self, agent_id: &str, msg: &str) -> P4n4Result<ChatResponse>;
    pub async fn ping(&self) -> P4n4Result<u64>;
}
```

#### `EdgeClient`

```rust
impl EdgeClient {
    pub async fn run_inference(&self, req: &InferenceRequest) -> P4n4Result<InferenceResult>;
    pub async fn ping(&self) -> P4n4Result<u64>;
}
```

---

## 5. Feature Flags

```
default = ["secrets", "manifest"]
```

| Feature | Enables | Pulls in |
|---------|---------|----------|
| `secrets` | `SecretGenerator` | `rand`, `base64`, `hex` |
| `manifest` | `P4n4Manifest` | `serde`, `serde_json` |
| `auth` | `JwtCodec`, `ApiKeyHasher` | `jsonwebtoken`, `argon2`, `rand`, `hex` |
| `clients` | All `clients::*` | `reqwest`, `rumqttc`, `tokio`, `serde`, `serde_json` |
| `python` | PyO3 extension module | `pyo3`, + `secrets`, `manifest`, `auth` |
| `full` | Everything | All of the above |

`models` is always compiled (no optional deps).

### Which consumer uses which features

| Consumer | Feature set |
|----------|-------------|
| `p4n4-api` (Rust) | `full` — needs all clients + auth |
| `p4n4-cli` (Python wheel) | `python` — secrets, manifest, auth; no async clients |

The CLI's runtime Docker interactions continue to use Python subprocess wrappers
in `p4n4-cli/p4n4/utils/docker.py`. The `clients` feature is not included in
the Python wheel to keep the wheel small and dependency-free (no Tokio runtime
embedded).

---

## 6. Consuming the Library

### As a Rust dependency (`p4n4-api`)

```toml
# p4n4-api/Cargo.toml
[dependencies]
p4n4-lib = { path = "../p4n4-lib", features = ["full"] }
# or, once published to crates.io:
# p4n4-lib = { version = "0.1", features = ["full"] }
```

Usage example:

```rust
use p4n4lib::{
    models::TelemetryPoint,
    clients::{InfluxClient, InfluxConfig},
    auth::{JwtCodec, Role},
    secrets::SecretGenerator,
};

let client = InfluxClient::new(InfluxConfig { .. });
let key = SecretGenerator::api_key();
let jwt = JwtCodec::new(&secret, 3600, 604800)
    .encode("device:T001", Role::Device)?;
```

### As a Python package (`p4n4-cli`)

The wheel is built by `maturin` and installed alongside the `p4n4` CLI package.
It is declared as a dependency in `p4n4-cli/pyproject.toml`:

```toml
[project]
dependencies = [
  "p4n4lib>=0.1",
  ...
]
```

Usage example:

```python
from p4n4lib import SecretGenerator, P4n4Manifest

key = SecretGenerator.api_key()
manifest = P4n4Manifest.load(".p4n4.json")
manifest.validate()
```

The Python module name is `p4n4lib` (importable); the native extension submodule
is `p4n4lib._p4n4lib` (internal, not used directly).

---

## 7. Python Bindings (PyO3 / maturin)

### What is exposed

Only pure, synchronous functionality is exposed to Python — no async clients,
no Tokio runtime. This keeps the wheel self-contained.

| Python class/function | Rust source |
|-----------------------|-------------|
| `SecretGenerator` | `src/secrets.rs` |
| `P4n4Manifest` | `src/manifest/mod.rs` |
| `TelemetryPoint` | `src/models/telemetry.rs` |
| `Device`, `NewDevice` | `src/models/device.rs` |
| `InferenceRequest`, `InferenceResult` | `src/models/inference.rs` |

Auth types (`JwtCodec`, `ApiKeyHasher`) are **not** exposed — the CLI never
issues JWTs or hashes API keys; that logic belongs in the API.

### `src/python.rs` structure

```rust
use pyo3::prelude::*;

#[pymodule]
fn _p4n4lib(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_class::<PySecretGenerator>()?;
    m.add_class::<PyP4n4Manifest>()?;
    m.add_class::<PyTelemetryPoint>()?;
    m.add_class::<PyDevice>()?;
    // ...
    Ok(())
}
```

Each Python-facing type is a thin `#[pyclass]` wrapper that delegates to the
corresponding Rust struct. Rust errors are converted to `PyErr` (raises
`ValueError` or `IOError` in Python) via a `From<P4n4Error> for PyErr` impl.

### Building the wheel

```bash
# Development (editable install into active venv)
maturin develop --features python

# Build wheel for distribution
maturin build --release --features python
```

`maturin` reads `pyproject.toml` for package metadata and `Cargo.toml` for the
crate. The `python-source = "python"` directive places the compiled `.so` next
to `python/p4n4lib/__init__.py` for local imports.

---

## 8. Error Handling

A single error type propagates through the entire library:

```rust
// src/error.rs

#[derive(Debug, thiserror::Error)]
pub enum P4n4Error {
    #[error("manifest error: {0}")]
    Manifest(String),

    #[error("auth error: {0}")]
    Auth(String),

    #[error("client error ({service}): {message}")]
    Client { service: &'static str, message: String },

    #[error("serialization error: {0}")]
    Serde(#[from] serde_json::Error),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("secret generation error: {0}")]
    Secret(String),
}

pub type P4n4Result<T> = Result<T, P4n4Error>;
```

Consumer-specific error mapping:
- `p4n4-api`: maps `P4n4Error` → `ApiError` → `(StatusCode, JSON)` in `error.rs`
- `p4n4-cli` (Python): maps `P4n4Error` → `PyErr` in `src/python.rs`

---

## 9. Build & Development Setup

### Prerequisites

| Tool | Minimum version | Purpose |
|------|----------------|---------|
| Rust (stable) | 1.80 | Compile crate |
| `maturin` | 1.5 | Build Python wheel |
| Python | 3.11 | Develop / test Python bindings |
| `cargo-nextest` | latest | Faster test runner (optional) |

### First-time setup

```bash
# Install maturin
pip install maturin

# Build and install the Python extension into the current venv (editable)
maturin develop --features python

# Run Rust unit tests
cargo test

# Run Rust tests with nextest
cargo nextest run
```

### Feature-specific builds

```bash
# Build without clients (default — what p4n4-cli wheel uses)
cargo build --features "secrets,manifest,python"

# Build everything (what p4n4-api links against)
cargo build --features full

# Check without building
cargo check --features full
```

### Linting

```bash
cargo fmt --check
cargo clippy --features full -- -D warnings
```

---

## 10. Testing Strategy

### Unit tests (in-source, `#[cfg(test)]`)

Cover all pure logic: secret format validation, manifest round-trips,
JWT encode/decode, argon2 hash/verify. No network calls, no file I/O beyond
`tempfile`.

### Integration tests (`tests/`)

| File | What it tests | Requires |
|------|--------------|----------|
| `test_secrets.rs` | Format, length, uniqueness of generated values | Nothing |
| `test_manifest.rs` | Load/save/validate `.p4n4.json` via `tempfile` | Nothing |
| `test_auth.rs` | JWT roundtrip, expiry, role enforcement, argon2 | Nothing |
| `test_clients.rs` | Live calls to each service client | Running p4n4-iot + p4n4-ai + p4n4-edge |

Client integration tests are gated behind a `#[cfg(feature = "clients")]`
guard and the environment variable `P4N4_INTEGRATION=1`. They are skipped in
normal CI and run only in the dedicated `integration` workflow.

### Python binding tests

A pytest suite lives in `python/tests/`:

```
python/
└── tests/
    ├── test_secrets.py
    ├── test_manifest.py
    └── test_models.py
```

Run after `maturin develop`:

```bash
pytest python/tests/ -v
```

These are run in CI after the wheel build step.

---

## 11. Versioning & Compatibility

`p4n4-lib` follows **semver**. The Rust crate version and the Python wheel
version are always kept in sync (both live in `Cargo.toml`; `maturin` reads it).

### Compatibility contract

| Semver bump | Reason |
|-------------|--------|
| `PATCH` | Bug fixes with no API change |
| `MINOR` | New public types or methods; backward-compatible feature additions |
| `MAJOR` | Removed or renamed public items; changed method signatures |

### Consumer pin strategy

- `p4n4-api` pins `p4n4-lib` with `=0.1.x` in `Cargo.toml` and updates
  explicitly when a new minor is released.
- `p4n4-cli` pins `p4n4lib>=0.1,<0.2` in `pyproject.toml`.

### Compatibility table (maintained in `src/compat.rs`)

```rust
/// Minimum p4n4-lib version required by each consumer at their current release.
pub const API_MIN_LIB: &str = "0.1.0";
pub const CLI_MIN_LIB: &str = "0.1.0";
```

---

## 12. CI/CD

### GitHub Actions

```
.github/workflows/
├── ci.yml          ← runs on every push + PR
├── integration.yml ← runs on push to main (requires service containers)
└── publish.yml     ← runs on version tag v*.*.*
```

#### `ci.yml`

```yaml
jobs:
  rust:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
    steps:
      - cargo fmt --check
      - cargo clippy --features full -- -D warnings
      - cargo test --features "secrets,manifest,auth"  # no clients (no services)
      - cargo build --release --features full

  python:
    steps:
      - maturin develop --features python
      - pytest python/tests/ -v
```

#### `integration.yml`

Spins up `p4n4-iot` + `p4n4-ai` + `p4n4-edge` via Docker Compose before running:

```bash
P4N4_INTEGRATION=1 cargo test --features clients
```

#### `publish.yml`

Triggered by `git tag v0.x.y && git push --tags`:

1. `cargo publish` — publishes to crates.io
2. `maturin publish` — builds wheels for `linux/amd64`, `linux/arm64`, `macos`, `windows` via QEMU and uploads to PyPI

```yaml
- uses: PyO3/maturin-action@v1
  with:
    command: publish
    args: --features python
  env:
    MATURIN_PYPI_TOKEN: ${{ secrets.PYPI_API_TOKEN }}
```

---

*Maintained in `raisga/p4n4-lib`. For questions, open an issue or PR.*
