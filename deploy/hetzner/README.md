# Hetzner Package (Phase 1)

Status: experimental and not verified on a fresh remote host. One-click provisioning and recovery below are acceptance targets, not a production support guarantee. See [the support matrix](../../docs/support-matrix.md).

## Objective
Deliver a one-click, non-HA deployment on a fresh Hetzner server.

## Topology
- Single VM host.
- Single deployment stack with local `postgres`, `clickhouse`, and `llama.cpp` inference service.
- Persistent host storage for data and caches.

## Required Deliverables
- `coolify.yaml`: app definition for Coolify-managed deployment.
- `docker-compose.prod.yml`: production service profile without dev-only services.
- `bootstrap.sh`: fresh-host provisioning script:
  - install Docker and Docker Compose
  - install/start Coolify
  - create required directories/volumes
  - inject environment and deploy stack
  - run migration and readiness checks
- `launch.md`: one-click cloud-init launch contract and deploy-button wiring notes.

## One-Click Flow
1. Provision server.
2. Run bootstrap action once.
3. Bootstrap installs prerequisites and deploys stack.
4. Validate `/health` and return URLs.

## Acceptance Criteria
- Fresh host reaches running state without manual package install steps.
- All required services from `deploy/common/service-profile.md` are running.
- Host restart preserves data and recovers services automatically.

## Files
- `deploy/hetzner/docker-compose.prod.yml`
- `deploy/hetzner/bootstrap.sh`
- `deploy/hetzner/launch.sh`
- `deploy/hetzner/install.sh`
- `deploy/hetzner/coolify.yaml`
- `deploy/hetzner/launch.md`

## App routes
- `/tutorials`
- `/tutorials/install-and-configure-master-builder-on-hetzner-linux`
- `/tutorials/hetzner-setup`
