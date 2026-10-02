# pgfire

[![CI](https://github.com/omar16100/pgfire/actions/workflows/ci.yml/badge.svg)](https://github.com/omar16100/pgfire/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

PostgreSQL 17 with a C durability layer that stores its base snapshot and write-ahead log in Firestore. Penpot uses the normal PostgreSQL protocol, SQL executor, catalogs, constraints, extensions and locks. A successful application commit waits for a Firestore publication. Local PostgreSQL files are disposable.

This is a working local prototype, tested against Penpot 2.18.0 and the official Firestore emulator. It is not a production Cloud Run deployment. See [verification](docs/verification.md) for measured results and remaining limits.

## Design

```
Penpot / pgjdbc → admission proxy → PostgreSQL C core
                                     │ synchronous physical replication
                                     ▼
                                pgfire C receiver
                                     │ HTTPS REST commits
                                     ▼
                         Firestore snapshot + WAL chunks
```

- Immutable, SHA-256-checked objects use 512 KiB chunks. Payloads can exceed a Firestore document or API request limit because they are staged before publication.
- One atomic Firestore commit publishes a WAL record and advances the database head. The head has an `updateTime` precondition, so a displaced owner cannot publish.
- The C receiver reports PostgreSQL's flush position only after that commit succeeds. Unknown HTTP outcomes stop service; they are resolved by reading the durable head during recovery.
- A PostgreSQL preload extension closes a client connection if a canceled synchronous wait would otherwise return success without a durable confirmation. It rejects asynchronous commits, unlogged tables and prepared transactions.
- A 90-second owner lease, conservative monotonic deadline, and independent supervisor watchdog fence service. New containers acquire ownership before recovery and before opening the public SQL port.
- Recovery downloads the snapshot and contiguous WAL chain, verifies hashes, reconstructs WAL segments and timeline histories, then lets PostgreSQL replay. The public port opens after recovery and synchronous streaming are ready.
- Recovery automatically checkpoints an incomplete trailing WAL record after PostgreSQL has replayed its complete prefix. An optional offline checkpoint also publishes a new clean snapshot during startup, before opening the public port. Old snapshots and WAL remain unreferenced; garbage collection is not yet implemented.

The engine and guard are C. Python handles process supervision and local test orchestration. No Penpot source changes, SQL translation, migration skipping, or Cloud SQL instance are involved. Penpot uploads and exported assets still use Penpot's separate filesystem/object-storage service; its database blobs use normal `bytea` and WAL.

## Build and run locally

Prepare Python dependencies and pinned test inputs:

```sh
git clone https://github.com/omar16100/pgfire.git
cd pgfire
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python tools/penpot/prepare.py
```

On macOS with Apple's `container` CLI installed:

```sh
container system start
container build --progress plain -t pgfire:local -m 2G .
.venv/bin/python tools/penpot/apple_stack.py up
```

The helper discovers container addresses and starts the emulator, engine, Valkey, and Penpot backend/frontend/exporter. Initial JVM startup can take several minutes. It uses a new local account created in Penpot's registration screen. All credentials in this stack are synthetic local test credentials.

- Penpot: <http://localhost:59001>
- PostgreSQL: `127.0.0.1:55434`, database/user `penpot`, password `pgfire-local-test`
- Firestore emulator: `127.0.0.1:58080`

For Docker, the equivalent configuration is supplied in [compose.yaml](compose.yaml):

```sh
docker compose up --build -d
```

The Apple container path was exercised for the acceptance results; the Docker Compose application stack has not yet been exercised end to end. CI builds the Linux engine image and validates the Compose configuration.

The emulator itself stores data in memory. Keep its container running during recovery tests. Stopping it loses the synthetic database. To restart the engine, **recreate its container**, keeping the same Firestore project/database/namespace. Do not reuse an old PGDATA directory. The supervisor intentionally refuses an existing cache.

After a manual Apple engine replacement, update the application's database address:

```sh
.venv/bin/python tools/penpot/apple_stack.py refresh-app
```

For Docker, use `docker compose up -d --force-recreate engine`; the service name stays stable. SQL clients retry connections while the replacement waits for a previous lease to expire.

## Test

Build the C utilities with sanitizers on macOS:

```sh
brew install cmake ninja libpq json-c openssl@3
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug -DPGFIRE_SANITIZE=ON \
  '-DCMAKE_PREFIX_PATH=/opt/homebrew/opt/libpq;/opt/homebrew/opt/openssl@3;/opt/homebrew/opt/json-c'
cmake --build build
ctest --test-dir build --output-on-failure
FIRESTORE_EMULATOR_HOST=127.0.0.1:58080 PGFIRE_NAMESPACE=fencing-test build/test_fencing
.venv/bin/python tests/integration/test_store.py
.venv/bin/python tests/integration/test_engine.py
PGFIRE_TEST_EMULATOR=FIRESTORE_IP:8080 .venv/bin/python tests/integration/test_watchdog.py
.venv/bin/python tests/integration/test_partial_wal.py --emulator FIRESTORE_IP:8080
```

The Linux image builds the C program and PostgreSQL extension and runs the codec test. Store tests create isolated Firestore namespaces. Engine tests use an isolated `pgfire_acceptance` schema and deliberately pause the receiver; run them against the disposable test stack only.

Run the **unmodified upstream Penpot backend tests** using the release JVM and dependencies:

```sh
.venv/bin/python tools/penpot/apple_stack.py status
# Replace the addresses below with the status output.
.venv/bin/python tools/penpot/run_suite.py \
  --database-uri postgresql://ENGINE_IP:5432/penpot_test \
  --redis-uri redis://VALKEY_IP/2 --name pgfire-penpot-suite
```

This resets the `penpot_test` database. The same runner accepts an ordinary PostgreSQL instance for the baseline. It loads all 51 `*_test.clj` namespaces from tag 2.18.0, invokes `clojure.test`, and returns a failing exit status for any test failure or error. It does not run the frontend or render-WASM test suites.

Crash recovery destroys the entire engine container, including all local database files. Do not run it concurrently with the backend suite:

```sh
.venv/bin/python tools/penpot/crash_probe.py --emulator FIRESTORE_IP:8080 --rounds 2
.venv/bin/python tools/penpot/crash_probe.py --emulator FIRESTORE_IP:8080 --rounds 2 --checkpoint
.venv/bin/python tools/penpot/apple_stack.py refresh-app
```

With the app ready, exercise its HTTP workflow (creates synthetic local account/project data):

```sh
.venv/bin/python tools/penpot/http_smoke.py
.venv/bin/python tools/penpot/http_render.py
```

The seeded local login is `pgfire.acceptance@example.com` / `Pgfire-local-only-2026!`. The workflow saves a rectangle and comment, reloads them, and downloads a verified `.penpot` archive. PNG export uses Penpot's own exporter service. These API checks do not replace a browser interaction test.

Logs and machine-readable results are written under `artifacts/`. The SQL inventory proxy and fingerprint summarizer are under `tools/inventory/`; captured parameter values and passwords are omitted, but SQL literals can still contain data, so use synthetic fixtures.

GitHub Actions builds the C utilities with ASan/UBSan, runs the codec and Firestore emulator store/fencing tests, checks Python syntax, and builds the Linux engine image including its PostgreSQL guard. The full Penpot suite and destructive recovery scenarios are separate local acceptance tests described above.

## Configuration and limits

| Variable | Meaning |
|---|---|
| `FIRESTORE_EMULATOR_HOST` | Local emulator host:port; uses HTTP without authentication |
| `PGFIRE_ACCESS_TOKEN` | Explicit Google OAuth token when using the live HTTPS Firestore endpoint |
| `PGFIRE_PROJECT` | Firestore project, default `pgfire-local` |
| `PGFIRE_DATABASE` | Firestore database, default `(default)` |
| `PGFIRE_NAMESPACE` | Independent database root under `pgfire/{namespace}` |
| `PGFIRE_PASSWORD` | Initial application-role password; later starts restore the existing role |
| `PGFIRE_CHECKPOINT_ON_RECOVERY=1` | Publish a clean replacement snapshot before accepting connections |

Internal variables `PGFIRE_OWNER_TOKEN`, `PGFIRE_LEASE_FILE`, `PGFIRE_RECOVERY_FORK_LSN`, and `PGFIRE_CONNINFO` belong to the supervisor/receiver protocol. Do not configure competing receivers with the same owner token. Local PostgreSQL superuser access is a trusted administrative boundary.

Current limits: one writable owner, PostgreSQL 17 and 16 MiB WAL segments, no rolling major-version upgrades, no replicas, no point-in-time restore interface, no automatic object garbage collection. Retention is capped at 100,000 WAL records between checkpoints; reaching the cap stops service before acknowledging further WAL. Enable startup checkpoints and recreate the container before reaching the limit. Checkpoints require downtime.

Every WAL batch updates a single Firestore head document, making that document a throughput bottleneck. The emulator cannot establish production latency, contention, quotas, cost, or durability. OAuth token refresh/metadata-server authentication, durable Penpot asset storage, production TLS/IAM, deployment lifecycle, and load/fault testing against live Firestore remain deployment work. Lease expiry assumes clocks are synchronized within the conservative safety margin. SQL visibility follows PostgreSQL synchronous replication: another session can see a locally committed transaction while its originating client is still waiting for durable acknowledgement. A static access token expires and causes service to stop safely. Do not treat the local stack's open registration or test credentials as deployment configuration.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and validation guidance and [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

Original pgfire code is licensed under [Apache 2.0](LICENSE). Dependencies and the captured Penpot SQL inventory retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md). This is an independent project, unaffiliated with PostgreSQL, Google, or Penpot.
