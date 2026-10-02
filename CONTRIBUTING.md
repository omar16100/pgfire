# Contributing to pgfire

pgfire is an experimental PostgreSQL 17 durability layer for Firestore. Contributions are welcome through GitHub issues and pull requests. Describe a concrete problem and include a small reproduction when possible.

## Development

Follow the build and local stack instructions in [README.md](README.md). CMake builds the receiver and store utilities; the PostgreSQL guard uses PGXS and is built by the Dockerfile. Python supervises PostgreSQL and drives the local acceptance harness.

Before submitting a change:

- Run the codec test with ASan/UBSan and the emulator store/fencing tests for C storage changes.
- Run the database integration tests for SQL acknowledgement or guard changes.
- Exercise crash recovery, receiver stalls, and incomplete WAL recovery for ownership, replication, or recovery changes.
- Run the upstream Penpot backend suite for compatibility changes.
- Include the commands and results in the pull request, and state which checks were not run.

The acceptance harness uses synthetic data and can destroy its engine containers. Run it in a disposable local stack. Keep the Firestore emulator running during recovery tests because its contents are in memory.

GitHub Actions checks the native build, codec, emulator storage and fencing, Python syntax, Linux image build, and Compose syntax. It does not run the full Penpot suite or establish live Firestore durability.

## Durability changes

A successful application COMMIT must follow durable Firestore publication. Preserve the compare-and-swap head update, lease fencing, checksum verification, independent watchdog, and closed SQL listener during recovery. Add a failure reproduction when changing any of these boundaries.

Do not commit credentials, local database files, downloaded dependencies, browser/session data, or raw application captures. Local test output belongs in the ignored `artifacts/` directory. Only reviewed, synthetic results belong in [docs/results](docs/results).

## License

Contributions to original pgfire files are made under the [Apache License 2.0](LICENSE). Preserve upstream notices and licenses for third-party material described in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
