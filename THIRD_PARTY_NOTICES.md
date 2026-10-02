# Third-party notices

Original pgfire code is licensed under Apache 2.0. This license does not replace the licenses of dependencies or third-party material.

## Penpot SQL inventory

[tools/inventory/penpot-2.18.tsv](tools/inventory/penpot-2.18.tsv) contains SQL captured from Penpot 2.18.0, commit `5baffdc213f0deaaeb318e97a41d611ac0656a94`, including migration SQL and application queries. The capture was deduplicated, fingerprinted, and serialized as TSV with JSON-encoded SQL strings. It is provided as source text for compatibility analysis.

Copyright (c) KALEIDOS INC Sucursal en España SL and Penpot contributors.

This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. A copy is included in [LICENSES/MPL-2.0.txt](LICENSES/MPL-2.0.txt). The inventory remains under MPL 2.0, independently of pgfire's Apache 2.0 license.

Upstream source and notices: [Penpot 2.18.0](https://github.com/penpot/penpot/tree/5baffdc213f0deaaeb318e97a41d611ac0656a94). The preparation script downloads the unmodified upstream source separately into an ignored cache; the test runner loads that source without vendoring it here.

## Runtime and build dependencies

PostgreSQL is obtained from its official container image and retains the PostgreSQL License. libpq, libcurl, OpenSSL, json-c, Python, and the operating-system packages retain their respective licenses and packaged notices. Their source is not vendored in this repository. The image inherits PostgreSQL and Debian package license files from its base.

The local acceptance stack separately downloads or runs Penpot, Valkey, Eclipse Temurin, the Firestore emulator, and test dependencies. Each remains subject to its upstream terms; the pgfire license does not relicense them.
