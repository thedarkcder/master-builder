# Deployment Packaging Plan

This document is the execution contract for infrastructure packaging across providers.

## Decisions Locked
- Delivery order: Hetzner first, then AWS, then GCP.
- Hetzner: non-HA, bare-bones, one-click bootstrap from a fresh server.
- AWS: HA required.
- GCP: HA required.
- Runtime profile includes these services by default:
  - `api`
  - `admin-ui`
  - `run-worker`
  - `webhook-worker`
  - `project-automation`
  - `knowledge-sync`
  - `discord-gateway`
  - `discord-live-voice`
  - `llama-cpp`
  - `postgres`
  - `redis`
- Production excludes dev-only services by default:
  - `mailpit`
  - `tailscale`

## Provider Targets

### Hetzner (Phase 1)
- Objective: one-click non-HA deployment from a fresh Hetzner server.
- Packaging:
  - `deploy/hetzner/coolify.yaml`
  - documented Compose deployment bundle
  - bootstrap script for fresh host provisioning
- Data topology:
  - local `postgres` and `redis` containers on the same host
  - persistent volumes mounted on host storage
- Availability:
  - single node only
  - host reboot recovery required

### AWS (Phase 2)
- Objective: HA deployment with Marketplace-compatible launch flow.
- Compute:
  - ECS on EC2 Auto Scaling Groups across at least 2 AZs.
- Networking:
  - ALB in front of API/UI entrypoints.
- Data:
  - RDS PostgreSQL Multi-AZ
  - ElastiCache Redis with automatic failover
- Secrets and certs:
  - AWS Secrets Manager for secret material
  - ACM for TLS
- Packaging:
  - Terraform or CDK stack for HA runtime
  - Marketplace launch wrapper using AMI + CloudFormation

### GCP (Phase 3)
- Objective: single-region HA deployment.
- Compute:
  - Cloud Run services for `api` and `admin-ui`
  - Cloud Run worker pools for long-running workers
- Data:
  - Cloud SQL PostgreSQL with regional HA
  - Memorystore Redis HA tier
- Secrets and networking:
  - Secret Manager
  - VPC egress path for private service connectivity
- Packaging:
  - Terraform modules for runtime, data, secrets, and networking

## Cross-Provider Contracts
- A single required environment contract is defined in `deploy/common/env.required.md`.
- A single runtime service profile is defined in `deploy/common/service-profile.md`.
- Deployments must execute DB migrations before marking environment ready.
- Environments must expose `/health` and pass smoke checks before promotion.

## Validation Gates
- Startup gate:
  - all required services are healthy
  - migrations completed successfully
- Functional smoke gate:
  - API health endpoint succeeds
  - admin login path is reachable
  - one run is queued and processed
  - webhook processing succeeds
- Recovery gate:
  - restart/redeploy preserves persistent state
