# What Master Builder includes

Master Builder provides a workspace for planning work, running configured agent
workflows and inspecting their results. This guide describes interfaces present
in the source code at version `0.1.0`; it does not certify a complete production
delivery workflow. Permissions are route-specific. The
[support matrix](support-matrix.md#validation-boundaries) records platform and
provider validation limits, and the [readiness report](../OPEN_SOURCE_READINESS_REPORT.md)
records publication blockers.

## Work planning

Start planning from a project work board, inspect parent/engineering task progress
and review questions that stop work from continuing. Answer clarification through
the configured Jira or Discord follow-up, which can resume the associated workflow.
The board and execution views expose progress and questions; they do not provide
an in-page answer form.

Connect the project's Jira and agent runtime first. Parent planning has implemented
controls and typed planning steps, but its graph, architecture gate and projection
semantics retain [documented design gaps](architecture/parent-planning-workflow.md#current-code-comparison).
Do not treat the complete autonomous parent-planning lifecycle as validated.

Implementation: [project board](../admin-ui/components/project-parent-work-board.tsx),
[human input service](../orchestrator/core/runs/human_input_service.py).
See [decision rules](run-decision-engine.md#decision-gate-contract).

## Workflow execution

Start work, inspect operation attempts, cancel an active run, resume an eligible
execution, retry an eligible failed operation or request a fresh rerun. The UI
shows the controls permitted for the current execution and its recorded state.
Planning, development, test and review operations use configured agent profiles.

Execution requires separately running workers, authenticated agent runtimes,
authorized repository tools and the relevant platform toolchains. Isolate workers
before allowing them to execute repository code. An API health check does not
prove execution readiness.

Implementation: [workflow route handlers](../orchestrator/api/admin/workflows/routes.py),
[run inspection UI](<../admin-ui/app/(dashboard)/runs/[runId]/page.tsx>).
See [worker start decisions](run-decision-engine.md#worker-start-decision-matrix).

## Run inspection

Inspect stage outcomes, checkpoints, operation attempts, logs, agent activity,
tool calls and retained review evidence. Token analytics can filter recorded
usage by project, run, stage, attempt and model. Token analytics routes currently
require platform administration; run views have their own authorization rules.

The run UI's **Cost** view displays recorded input, uncached and output token
counts. It does not calculate monetary spending. Missing provider telemetry does
not establish zero usage, and these figures are not a billing record.

Implementation: [run inspection UI](<../admin-ui/app/(dashboard)/runs/[runId]/page.tsx>),
[token analytics routes](../orchestrator/api/routes/admin_tokens.py).
See [HTTP authorization contracts](public-contracts.md#http-interfaces).

## Project access

Create projects, connect repositories, invite members, resend or revoke invitations,
organize teams, change roles/permissions and manage project access requests. Project
administration also configures delivery policy, runtime settings and managed secret
references. Store credentials through authorized secret-management interfaces.

Actions depend on the acting principal's workspace/project permissions. Some
administrative routes require platform administration rather than a tenant role;
inspect the current API contract before granting access or building an integration.

Implementation: [team controls](../admin-ui/components/tenant-team-page.tsx),
[tenant and project routes](../orchestrator/api/routes/admin_tenants.py),
[managed secrets](../orchestrator/api/routes/admin_secrets.py).
See [configuration and persistence](public-contracts.md#configuration-and-persistence).

## Project knowledge

Upload documents, review/approve/reject knowledge assets, inspect extracted chunks
and facts, search indexed material and configure supported external sources.
Source controls expose synchronization results and manual synchronization where
the connector supports it.

Knowledge routes currently require platform administration. Indexing/retrieval
needs configured knowledge and embedding services; external connectors need their
own authenticated source access. Uploading a document alone does not establish
that retrieval or every source connector is working.

Implementation: [knowledge browser](../admin-ui/components/project-knowledge-browser-page.tsx),
[file uploads](../admin-ui/components/project-knowledge-add-section.tsx),
[source controls](../admin-ui/components/project-knowledge-sources-section.tsx),
[knowledge routes](../orchestrator/api/routes/admin_knowledge.py).
See [optional configuration](configuration.md#optional-integrations).

## Code review

Connect authorized GitHub repositories through a GitHub App installation. Delivery
workflows use branches and pull requests, and check results/review feedback feed
back into the run. Project policy and repository allowlists constrain access.
Maintainers remain responsible for authorizing execution, reviewing required
checks and deciding whether to merge.

Implementation: [GitHub webhook routes](../orchestrator/api/routes/webhook_github.py).
Follow [GitHub App setup](github-app-oauth-onboarding.md#github-app-creation-checklist)
and [delivery workflow setup](../README.md#using-delivery-workflows).
Moving this repository's own backlog to GitHub Issues does not replace the
application's configured Jira work intake.

## Preview QA

Configure applications and deployment environments, request a release, regenerate
a selected preview and inspect deployment logs. Application controls also expose
backup/restore requests. Their success depends on the configured host/provider;
a submitted request does not prove a deployment or recovery completed.

QA workflows can collect recordings against configured previews/builds. Reviewers
retrieve retained evidence through authenticated tenant/project/run-scoped routes.
Storage is private and subject to the configured retention policy.

Managed-host/Coolify deployments and browser capture need live validation. Mobile
capture requires its platform worker/toolchains and remains experimental. AWS/GCP
packages are design specifications; there is no complete supported installer.
Backup/restore controls are not a verified disaster-recovery guarantee.

Implementation: [application controls](../admin-ui/components/project-apps/project-app-admin-page.tsx),
[artifact delivery](../orchestrator/api/routes/qa_artifacts.py).
See [deployment status](support-matrix.md#validation-boundaries) and
[artifact access](configuration.md#qa-artifact-visibility).

## Discord collaboration

Connect project channels, receive delivery notifications, ask scoped project
questions, inspect queue/run status and start/retry/cancel permitted runs.
Clarification follow-ups associate answers with the relevant work.

Configure the Discord application, signature validation, authorized channels and
workspace permissions. Command availability follows the workspace's configured
integrations. Text commands and notifications are separate from experimental
voice delivery.

Implementation: [command dispatcher](../orchestrator/api/discord/commands/dispatcher.py),
[run controls](../orchestrator/api/discord/commands/run_controls.py).
See [Discord command ingress](run-decision-engine.md#discord-commands).

## Team briefings

Configure stand-up and retrospective voice briefings: enable/disable them, select
a timezone, weekdays/time and reporting window, queue a briefing now and inspect
its execution history. Configuration/scheduling routes currently require platform
administration.

The supported automation kinds are `standup_voice_brief` and `retro_voice_brief`.
Briefings use the configured Pocket TTS provider and model dependencies to create
WAV audio, then post it as an attachment in the Discord project channel. This path
does not use live Discord voice transport. It requires the TTS setup and an
authorized bot/channel, plus a real end-to-end delivery test. Briefings remain
experimental; the scheduling/queue interface is not proof of successful delivery.

Implementation: [briefing controls](../admin-ui/components/tenant-project-discord-page.tsx),
[automation scheduling](../orchestrator/core/projects/automation_service.py),
[audio attachment delivery](../orchestrator/core/projects/automation_execution_service.py),
[TTS provider](../orchestrator/core/voice/tts.py).
See [optional integration prerequisites](configuration.md#optional-integrations) and
[platform status](support-matrix.md#validation-boundaries).

## Development direction

The [features directory](../features) contains design proposals and historical
plans. A document there is not evidence that a feature has shipped. Current
repository work belongs in [GitHub Issues](https://github.com/thedarkcder/master-builder/issues);
the historical Jira import is pending source access. Claims on the homepage should
follow implemented interfaces and the validation boundaries above.
