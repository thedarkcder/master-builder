# Deployment Workspace

Status: Hetzner provisioning is experimental and unverified; AWS/GCP directories are design specifications without complete installers. See [the support matrix](../docs/support-matrix.md).
This directory contains provider-specific deployment packages and shared runtime contracts.

## Structure
- `common/`: contracts shared by all providers
- `hetzner/`: Hetzner non-HA one-click package (phase 1)
- `aws/`: AWS HA package (phase 2)
- `gcp/`: GCP HA package (phase 3)

## Execution Order
1. Implement and validate `hetzner/`.
2. Implement and validate `aws/`.
3. Implement and validate `gcp/`.

## Source of Truth
- Delivery and architecture decisions are defined in:
  - `docs/deployment-packaging.md`
- Shared contracts are defined in:
  - `deploy/common/env.required.md`
  - `deploy/common/service-profile.md`
