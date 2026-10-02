# Security policy

pgfire is a prototype. No release is currently supported for production use. Testing has used the official Firestore emulator; live Firestore authentication, quotas, latency, and deployment behavior remain unverified.

Report vulnerabilities privately using [GitHub private vulnerability reporting](https://github.com/omar16100/pgfire/security/advisories/new). Include a minimal reproduction, affected revision, and the observed effect on confidentiality, acknowledgement, recovery, or fencing. Do not include access tokens, passwords, or real database contents.

The Compose and Apple container configurations are local acceptance fixtures. Their passwords, signing key, open registration, and HTTP endpoints are intentionally public test values. Do not use them for a deployment containing real data.

See the [configuration and limits](README.md#configuration-and-limits) before experimenting with live infrastructure. Local PostgreSQL superuser access is a trusted administrative boundary.
