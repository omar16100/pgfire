# Verification — 1 October 2026

Validation uses synthetic local data, PostgreSQL 17, and the official Firestore emulator. No GCP resources were created. Reviewed machine-readable summaries are checked into [results](results); raw local logs, session data, and downloaded dependencies are excluded. These results predate public repository packaging and CI.

## Pinned inputs

- Penpot source tag **2.18.0**, commit `5baffdc213f0deaaeb318e97a41d611ac0656a94`; production classes/dependencies from `penpotapp/backend:2.18.0`.
- PostgreSQL **17.11**, base OCI index `sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652`.
- Official Firestore emulator **1.22.0**, SHA-256 `9b6498b7f62714d67f48f59b3818883cd682dbcd46b9f59511de81c97bb5166c`.
- Valkey **8.1.10**; libpq **18.6**; psycopg **3.2.12**; pglast **7.10**.
- macOS arm64 host, Apple's container runtime, Linux arm64 engine. C17 native ASan/UBSan build plus Linux GCC build and PostgreSQL PGXS extension.

## Backend compatibility

All 51 upstream `*_test.clj` namespaces were loaded unchanged. The runner uses `clojure.test` with the release JAR rather than invoking upstream's Kaocha launcher. Both targets use the same runner, tests, dependency image and separate test databases.

| Target | Tests | Assertions | Failures | Errors | Test execution time |
|---|---:|---:|---:|---:|---:|
| Ordinary PostgreSQL baseline | 771 | 4,793 | 0 | 0 | 188.3 s |
| pgfire + Firestore emulator, initial complete run | 771 | 4,793 | 0 | 0 | 467.7 s |
| Final pgfire image + Firestore emulator | 771 | 4,793 | 0 | 0 | 221.8 s |

Times exclude JVM namespace loading. The initial pgfire run overlapped image builds and app startup on a memory-constrained host; these are observations, not controlled performance measurements.

The suite covers database operations, authentication and profiles, teams/projects/files, comments, permissions, snapshots, thumbnails, media/storage, import/export, deletion, telemetry tasks and utilities. The standalone Penpot backend also applied its migrations through pgfire. Migration records were not prefilled and no DDL or stored function was substituted.

Evidence:

- [Baseline result](results/penpot-baseline.edn)
- [pgfire result](results/penpot-initial.edn)
- [Final build result](results/penpot-final.edn)
- [Image and source hashes](results/build-manifest.json)

## Durability and engine checks

The native codec test passed with ASan and UBSan. Four emulator object-store tests passed: empty/small/chunked objects including an object over 11 MiB; corrupt chunk rejection and removal of partial output; competing owner rejection and token adoption; preservation of an existing output file. The fencing executable verifies that a stale `updateTime` cannot publish after a new owner generation, and checks the retained-WAL admission limit.

Five database integration tests passed: concurrent UPSERT increments, `SKIP LOCKED`, a multi-megabyte random binary transaction, rejection of asynchronous/unlogged durability bypasses, and canceled-commit handling. In the cancellation test, the receiver is paused before a write, the write cannot return, and cancellation closes the client connection without a successful commit acknowledgement. Other connections remain usable after the receiver resumes.

Two `SIGKILL` + full-container-deletion recoveries passed, reconstructing acknowledged random binary rows and Penpot table counts from Firestore alone. The timeline advanced from 2 → 3 → 4 and new writes succeeded after each recovery. Recovery took 303.0 s and 143.2 s, including the deliberately unexpired owner lease.

The receiver-stall test sent `SIGSTOP` to an isolated engine’s receiver. SQL reads stopped after 81.2 s with 79.9 s remaining on the conservative deadline (the probe has a 2 s connection timeout). The watchdog acts independently of the receiver.

Two more destructive recoveries with offline checkpoint replacement passed (timelines 4 → 5 → 6), taking 126.6 s and 104.8 s including lease expiry. Both verified all acknowledged probe rows and the Penpot tables, then accepted new writes.

A separate fault test killed the engine during a 64 MiB uncommitted insert after WAL had been published at an intermediate page boundary. This exposed an incomplete trailing record: PostgreSQL correctly recovered the last complete record, but the initial receiver rejected the shortened timeline fork. The fix detects that case, publishes a clean snapshot under the same fenced owner, and resumes streaming. Replaying the exact captured failure passed: the previously acknowledged row survived, the uncommitted row was absent, and a new write succeeded.

One cancellation-test rerun hit the Apple CLI harness’s 10-second timeout while resuming the paused receiver. The supervisor then stopped SQL at its lease deadline as designed. Increasing the harness command timeout and rerunning the five engine tests passed. A separate graceful-shutdown test confirmed ownership was released after SQL stopped (0.44 s). Interactive browser acceptance was not completed because the browser automation connection detached while Penpot loaded. This is not recorded as a browser pass.

- [Store tests](results/store-tests.txt)
- [Fencing tests](results/fencing-tests.txt)
- [Database tests](results/engine-tests.txt)
- [Crash recovery results](results/crash-recovery.json)
- [Lease watchdog result](results/watchdog-test.json)
- [Checkpoint recovery results](results/checkpoint-recovery.json)
- [Interrupted large-transaction recovery](results/partial-wal.json)
- [Graceful shutdown result](results/shutdown-test.json)

## HTTP application workflow

The running Penpot frontend/backend completed registration and password login, then created a team, project and file through its public HTTP API. `update-file` saved a named blue rectangle and renamed its page; a subsequent `get-file` returned those changes. Creating and reading a comment also passed. The `export-binfile` event stream completed and the downloaded `.penpot` ZIP passed CRC checks and contained the saved rectangle.

PNG export through Penpot’s exporter also passed: its bundled renderer produced the expected 320 × 180 blue rectangle.

This exercises the actual HTTP service, sessions, database, file serialization, rendering and shared asset storage. It is separate from a browser click-through test. The synthetic login is `pgfire.acceptance@example.com` / `Pgfire-local-only-2026!` in the local app only.

- [HTTP workflow result](results/http-smoke.json)
- [PNG export result](results/http-render.json)
- [Rendered rectangle](results/pgfire-rectangle.png)

## Observed wire behavior

A logging proxy captured baseline application startup, background work and part of the upstream suite. The complete baseline suite then ran directly for comparable timing. The inventory is **observed coverage, not a complete catalog of every possible Penpot statement**.

The capture contained 531 connections, 687 exact SQL strings, 663 parser fingerprints, 964 named statement/session pairs, text and binary formats, and Execute row limits of 0, 1 and 1,000. There were no capture errors. The 60 SQLSTATE `57P01` responses came from intentionally stopping the baseline server. Parameters and authentication secrets were not captured; SQL literals use synthetic fixtures.

- [Fingerprinted SQL](../tools/inventory/penpot-2.18.tsv)

## Boundaries

These tests do not establish production Firestore durability, quotas, cost, throughput or Cloud Run lifecycle behavior. They do not constitute a fuzzing campaign or the complete PostgreSQL regression suite. The engine intentionally rejects unlogged tables, prepared transactions and asynchronous commits. It retains native PostgreSQL SQL/locking behavior, but relies on a custom commit guard and Firestore publication path that require further review before production use.

Offline checkpoints compact the active recovery chain, but do not delete obsolete Firestore objects. Token refresh, production authentication/TLS, live Firestore testing and production deployment remain open. The supplied Docker Compose stack has not been executed here; the Apple container stack is the tested path.
