# GCP Package (Phase 3, HA)

## Objective
Deliver a single-region HA GCP deployment.

## Topology
- Cloud Run services for `api` and `admin-ui`.
- Cloud Run worker pools for long-running workers.
- Cloud SQL PostgreSQL with regional HA.
- Memorystore Redis HA tier.
- Secret Manager for runtime secrets.
- VPC egress configuration for private service connectivity.

## Required Deliverables
- Terraform stack that provisions:
  - Cloud Run services and worker pools
  - Cloud SQL and Memorystore
  - secret management and networking
  - service accounts and IAM bindings

## Availability Requirements
- Runtime remains available during single-zone disruption in region.
- Data services configured for HA failover behavior.

## Acceptance Criteria
- All required services are deployed and healthy.
- Rolling revision deployments preserve availability.
- Database and Redis failover scenarios are validated in staging.

