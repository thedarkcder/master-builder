# AWS Package (Phase 2, HA)

## Objective
Deliver an HA AWS deployment with ECS on EC2 ASG and Marketplace-compatible launch flow.

## Topology
- Multi-AZ VPC.
- ECS cluster backed by EC2 Auto Scaling Groups in at least 2 AZs.
- ALB for `api` and `admin-ui`.
- RDS PostgreSQL Multi-AZ.
- Approved ClickHouse deployment for product logs and audit queries.
- Secrets Manager for runtime secrets.

## Required Deliverables
- IaC stack (Terraform or CDK) that provisions:
  - networking, compute, runtime services, data, observability
- Marketplace wrapper assets for AMI + CloudFormation launch:
  - template parameters for sizing, domain, and secret references
  - bootstrap wiring to register runtime config and start services

## Availability Requirements
- Service continuity across single-AZ failure.
- Zero single points of failure in data plane.
- Rolling deploy support for API/UI without downtime.

## Acceptance Criteria
- All required services run with desired counts across at least 2 AZs.
- Simulated AZ impairment preserves API availability.
- Managed DB and ClickHouse failover behavior is validated.
