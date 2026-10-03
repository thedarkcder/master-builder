import Link from "next/link";
import { cookies } from "next/headers";
import type { Metadata } from "next";
import { ArrowRight, Blocks } from "lucide-react";

import { auth } from "@/auth";
import { GitHubStars } from "@/components/landing/github-stars";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { getLastWorkspaceCookieName } from "@/lib/workspace-preference";
import { REPOSITORY_URL as repositoryUrl } from "@/lib/public-repository";

const sourceUrl = `${repositoryUrl}/blob/main`;
const containerStyle = "mx-auto w-full max-w-6xl px-5 sm:px-8";
const linkStyle = "rounded-sm font-medium underline decoration-slate-300 underline-offset-4 hover:text-[#245ac7] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#245ac7]";
const sectionStyle = "border-t border-slate-200 py-12 sm:py-16";
const codeStyle = "min-w-0 overflow-x-auto rounded-md border border-[#263b59] bg-[#14243b] p-5 font-mono text-xs leading-7 text-[#e2eaf5] sm:text-sm";

export const metadata: Metadata = {
  title: "Master Builder — AI-assisted software delivery orchestration",
  description: "Jira intake, governed execution workers, workflow state, QA evidence and GitHub review. Technical overview, architecture and source setup for Master Builder.",
};

const mechanisms = [
  {
    id: "jira-intake",
    title: "Jira intake",
    body: "A workspace connects its Jira OAuth application and selects authorized projects. Ingress checks project scope and execution readiness before enqueueing a run. Delivery deduplication, issue locks and tenant concurrency limits govern admission.",
    links: [
      { label: "Jira onboarding", path: "docs/github-app-oauth-onboarding.md#jira--github-flow-summary" },
      { label: "Run decisions", path: "docs/run-decision-engine.md#enqueue-decision-matrix" },
    ],
  },
  {
    id: "worker-profiles",
    title: "Worker and runtime profiles",
    body: "Runtime configuration selects the agent adapter, model and execution settings. Workers require their own authenticated runtime, authorized repository tools and platform toolchains. Worker processes are started separately from the API; an API health check does not establish execution readiness.",
    links: [
      { label: "Runtime requirements", path: "docs/support-matrix.md#validation-boundaries" },
      { label: "Adapter and tool contracts", path: "docs/public-contracts.md#extensions-and-protocols" },
    ],
  },
  {
    id: "workflow-state",
    title: "Workflow state and decisions",
    body: "Runs persist queue status, stage checkpoints, decisions and outcomes. Worker selection rechecks readiness and concurrency before execution. Planning, development, test and review stages produce recorded results; blocked or failed work remains visible for operator action.",
    links: [
      { label: "Worker start decisions", path: "docs/run-decision-engine.md#worker-start-decision-matrix" },
      { label: "Command-line contracts", path: "docs/public-contracts.md#command-line" },
    ],
  },
  {
    id: "qa-evidence",
    title: "QA and evidence",
    body: "Run records retain stage results and review evidence. Optional QA workflows capture recordings against configured previews and worker platforms. Stored recordings are delivered through authenticated routes with tenant, project and run checks. Preview, browser and mobile proof require their own setup and validation.",
    links: [
      { label: "QA artifact access", path: "docs/configuration.md#qa-artifact-visibility" },
      { label: "QA validation boundaries", path: "docs/support-matrix.md#validation-boundaries" },
    ],
  },
  {
    id: "github-review",
    title: "GitHub review",
    body: "A GitHub App installation and repository allowlists define the repositories available to a workspace. Delivery runs use branches and pull requests, with checks and review feedback feeding the workflow. Project policy controls automation; review the policy before authorizing execution or merging.",
    links: [
      { label: "GitHub App setup", path: "docs/github-app-oauth-onboarding.md#github-app-creation-checklist" },
      { label: "Delivery workflow setup", path: "README.md#using-delivery-workflows" },
    ],
  },
  {
    id: "workspace-policy",
    title: "Workspace and policy",
    body: "Workspaces represent tenants with memberships, projects and repository scope. Administration configures project policy, integrations and managed secret references. Permissions are enforced by the relevant routes; configuration identifiers are separate from encrypted secret values.",
    links: [
      { label: "Configuration and persistence", path: "docs/public-contracts.md#configuration-and-persistence" },
      { label: "Core configuration", path: "docs/configuration.md#required-core-settings" },
    ],
  },
];

const components = [
  { name: "Administration UI", implementation: "Next.js · Auth.js", contract: "Browser sessions, workspace configuration and run inspection; server-side requests to the backend." },
  { name: "API", implementation: "FastAPI", contract: "Authentication, route validation, integration ingress and operator interfaces. The running backend exposes /docs and /openapi.json." },
  { name: "Operational state", implementation: "PostgreSQL · SQLAlchemy · Alembic", contract: "Durable run and configuration state. Apply supported migrations; database tables are internal contracts." },
  { name: "Execution workers", implementation: "Python · runtime adapters · repository tools", contract: "Queue selection and governed execution with configured runtime adapters and platform toolchains. Operators must isolate repository execution." },
  { name: "Optional services", implementation: "ClickHouse · OpenTelemetry · S3-compatible storage", contract: "Telemetry and private QA artifacts. Temporal, voice and deployment integrations have separate prerequisites." },
];

export default async function HomePage() {
  const session = await auth();
  const cookieStore = await cookies();
  const preferredTenantId = cookieStore.get(getLastWorkspaceCookieName())?.value ?? null;
  const authenticatedHref = getDefaultAuthenticatedRoute(session?.user?.principal, { preferredTenantId });
  const accountHref = session ? authenticatedHref : "/login";
  const accountLabel = session ? "Open dashboard" : "Sign in";

  return (
    <div className="min-h-screen bg-[#f5f7fb] text-[#15243b]">
      <a href="#main-content" className="sr-only z-[60] rounded-md bg-[#245ac7] px-5 py-3 text-white focus:not-sr-only focus:fixed focus:left-4 focus:top-4">Skip to content</a>
      <header className="border-b border-[#ced8e6] bg-white">
        <div className={`${containerStyle} flex flex-wrap items-center justify-between gap-4 py-5`}>
          <Link href="/" className={`inline-flex items-center gap-3 font-mono text-sm tracking-tight ${linkStyle} no-underline`}><span className="flex h-8 w-8 items-center justify-center rounded border border-[#c8d7ee] bg-[#edf3fc] text-[#245ac7]"><Blocks aria-hidden="true" className="h-4 w-4" /></span>Master Builder</Link>
          <nav aria-label="Main navigation" className="flex flex-wrap items-center gap-4 text-sm sm:gap-5">
            <a href="#capabilities" className={`${linkStyle} no-underline`}>Capabilities</a>
            <a href="#architecture" className={`${linkStyle} no-underline`}>Architecture</a>
            <a href={`${sourceUrl}/README.md`} className={`${linkStyle} no-underline`}>Documentation</a>
            <Link href={accountHref} className={`${linkStyle} no-underline`}>{accountLabel}</Link>
            <GitHubStars />
          </nav>
        </div>
      </header>

      <main id="main-content" tabIndex={-1}>
        <section className="border-b border-[#d5deea] bg-white bg-[linear-gradient(to_right,#e7edf580_1px,transparent_1px),linear-gradient(to_bottom,#e7edf580_1px,transparent_1px)] bg-[size:28px_28px] py-12 sm:py-16">
          <div className={`${containerStyle} grid grid-cols-1 gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]`}>
            <div className="min-w-0">
              <p className="inline-block border-l-2 border-[#245ac7] bg-white px-3 py-1 font-mono text-xs text-[#4c6587]">Master Builder / technical overview</p>
              <h1 className="mt-5 max-w-3xl text-3xl font-semibold leading-tight tracking-tight sm:text-[2.75rem]">AI-assisted software delivery orchestration</h1>
              <p className="mt-6 max-w-3xl text-base leading-8 text-slate-600">A self-hosted service for Jira work intake, agent execution and GitHub review. Configure workspace policy and worker runtimes, then inspect persisted run state, stage results and evidence through the API and administration UI.</p>
              <div className="mt-7 flex flex-wrap items-center gap-5 text-sm">
                <a href={repositoryUrl} className="inline-flex items-center gap-2 rounded-md bg-[#245ac7] px-5 py-3 font-semibold text-white hover:bg-[#1d489e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#245ac7]">Get Started<ArrowRight aria-hidden="true" className="h-4 w-4" /></a>
                <a href="#source-setup" className={linkStyle}>Local setup</a>
                <a href={`${sourceUrl}/docs/public-contracts.md`} className={linkStyle}>Public contracts</a>
              </div>
            </div>
            <aside aria-label="Project facts" className="min-w-0 border border-[#cbd7e8] bg-[#f5f8fd] p-5 text-sm leading-7">
              <p className="mb-5 border-b border-[#d7e1ee] pb-3 font-mono text-xs uppercase tracking-wider text-[#4c6587]">Installation contract</p>
              <dl className="space-y-4">
                <div><dt className="text-slate-500">Project version</dt><dd className="font-mono">0.1.0 · early project</dd></div>
                <div><dt className="text-slate-500">Core stack</dt><dd>Python / FastAPI<br />PostgreSQL / Next.js</dd></div>
                <div><dt className="text-slate-500">Installation</dt><dd>Complete source checkout<br />Repository-built runtime image</dd></div>
                <div><dt className="text-slate-500">Project license</dt><dd><a href="#license" className={linkStyle}>AGPL-3.0-only</a></dd></div>
              </dl>
            </aside>
          </div>
        </section>

        <section id="capabilities" className={sectionStyle}>
          <div className={containerStyle}>
            <p className="mb-3 font-mono text-xs uppercase tracking-wider text-[#4c6587]">01 / Capabilities</p>
            <h2 className="text-2xl font-semibold tracking-tight">Delivery mechanisms</h2>
            <nav aria-label="Capability navigation" className="mt-5 flex flex-wrap gap-2 text-sm">
              {mechanisms.map((item) => <a key={item.id} href={`#${item.id}`} className={`${linkStyle} border border-[#cfdaea] bg-white px-3 py-2 text-xs no-underline`}>{item.title}</a>)}
            </nav>
            <div className="mt-8 divide-y divide-slate-200">
              {mechanisms.map((item, index) => (
                <article id={item.id} key={item.id} className="grid grid-cols-1 gap-4 py-7 md:grid-cols-[15rem_minmax(0,1fr)] md:gap-10">
                  <div><p className="font-mono text-xs text-[#245ac7]">MODULE / 0{index + 1}</p><h3 className="mt-2 text-lg font-semibold">{item.title}</h3></div>
                  <div><p className="max-w-3xl text-sm leading-7 text-slate-600">{item.body}</p><div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 text-sm">{item.links.map((link) => <a key={link.path} href={`${sourceUrl}/${link.path}`} className={linkStyle}>{link.label}</a>)}</div></div>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section id="architecture" className={`${sectionStyle} bg-[#edf2f8]`}>
          <div className={containerStyle}>
            <p className="mb-3 font-mono text-xs uppercase tracking-wider text-[#4c6587]">02 / System layout</p>
            <h2 className="text-2xl font-semibold tracking-tight">Components and data flow</h2>
            <p className="mt-4 max-w-3xl text-sm leading-7 text-slate-600">The API owns route validation and ingress. PostgreSQL stores operational state. Workers select eligible runs and execute repository tools; the UI reads results through authenticated backend routes.</p>
            <ol aria-label="Run data flow" className="mt-7 grid grid-cols-1 gap-3 rounded-md border border-[#263b59] bg-[#14243b] p-5 text-sm text-[#e2eaf5] sm:grid-cols-5">
              {["Jira event or operator command", "Scope and readiness checks", "Persisted run queue", "Worker stages and artifacts", "Run inspection and GitHub review"].map((step, index) => <li key={step} className="min-w-0 border-l border-[#42628a] pl-3"><span className="block font-mono text-xs text-[#9ebbf5]">0{index + 1}</span><span className="mt-2 block leading-6">{step}</span></li>)}
            </ol>
            <p className="mt-3 font-mono text-xs text-[#4c6587]">Schematic / governed run flow · no worker readiness implied</p>
            <div className="mt-7 overflow-x-auto rounded-md border border-[#cfdaea] bg-white p-4">
              <table className="w-full min-w-[640px] text-left text-sm">
                <caption className="sr-only">Master Builder component responsibilities</caption>
                <thead><tr className="border-b border-slate-300"><th scope="col" className="py-3 pr-5 font-semibold">Component</th><th scope="col" className="py-3 pr-5 font-semibold">Implementation</th><th scope="col" className="py-3 font-semibold">Responsibility</th></tr></thead>
                <tbody>{components.map((component) => <tr key={component.name} className="border-b border-slate-200 align-top"><th scope="row" className="py-4 pr-5 font-medium">{component.name}</th><td className="py-4 pr-5 text-slate-600">{component.implementation}</td><td className="max-w-md py-4 leading-7 text-slate-600">{component.contract}</td></tr>)}</tbody>
              </table>
            </div>
            <div className="mt-6 flex flex-wrap gap-5 text-sm"><a href={`${sourceUrl}/README.md#architecture`} className={linkStyle}>Source layout</a><a href={`${sourceUrl}/docs/public-contracts.md#http-interfaces`} className={linkStyle}>HTTP interfaces</a></div>
          </div>
        </section>

        <section id="source-setup" className={sectionStyle}>
          <div className={containerStyle}>
            <p className="mb-3 font-mono text-xs uppercase tracking-wider text-[#4c6587]">03 / Local installation</p>
            <h2 className="text-2xl font-semibold tracking-tight">Source setup</h2>
            <p className="mt-4 max-w-3xl text-sm leading-7 text-slate-600">Use Python 3.11+, uv, Git, Node.js 20.9+ and Docker Compose v2. The local stack supplies PostgreSQL with vector support. The Python wheel alone does not install the required service assets.</p>
            <div className="mt-7 grid grid-cols-1 gap-7 lg:grid-cols-2">
              <div className="min-w-0"><h3 className="font-mono text-sm font-semibold">1. Source and local configuration</h3><pre className={`mt-3 ${codeStyle}`}><code>{`git clone ${repositoryUrl}.git
cd master-builder
uv sync --frozen --extra dev
uv run --frozen --extra dev python scripts/init_local_env.py`}</code></pre><p className="mt-3 text-sm leading-7 text-slate-600">The initializer generates private environment files and independent secrets. It refuses to overwrite existing configuration. Keep generated files private.</p></div>
              <div className="min-w-0"><h3 className="font-mono text-sm font-semibold">2. Core API and host UI</h3><pre className={`mt-3 ${codeStyle}`}><code>{`docker compose up --build -d api
curl --fail http://localhost:60001/health

# Start the UI in a second terminal:
cd admin-ui
npm ci
npm run dev`}</code></pre><p className="mt-3 text-sm leading-7 text-slate-600">Open localhost:60002/login with the generated administrator credentials. This starts the core API and UI; delivery runs additionally require your Jira/GitHub connections, runtime authentication and workers.</p></div>
            </div>
            <div className="mt-6 flex flex-wrap gap-5 text-sm"><a href={`${sourceUrl}/README.md#quick-start`} className={linkStyle}>Complete quick start</a><a href={`${sourceUrl}/docs/configuration.md`} className={linkStyle}>Environment configuration</a><a href={`${sourceUrl}/CONTRIBUTING.md`} className={linkStyle}>Development and tests</a></div>
          </div>
        </section>

        <section id="status" className={`${sectionStyle} bg-[#edf2f8]`}>
          <div className={`${containerStyle} grid grid-cols-1 gap-6 md:grid-cols-[15rem_minmax(0,1fr)] md:gap-10`}>
            <h2 className="text-2xl font-semibold tracking-tight">Status and boundaries</h2>
            <div className="max-w-3xl text-sm leading-7 text-slate-600">
              <p>Master Builder is at version 0.1.0. API schemas, workflow formats and extension interfaces are provisional. Pin a reviewed revision and rerun integration contracts on upgrades.</p>
              <p className="mt-4">Mobile QA, voice and provider deployment integrations are experimental. Temporal and local-model execution also require separate validation. AWS/GCP packages are design specifications. Core health and build checks do not prove authenticated delivery or live provider recovery.</p>
              <p className="mt-4">Workers execute repository code and development tools. Isolate execution environments and grant access only to authorized repositories. Review the support matrix and publication blockers before deploying.</p>
              <div className="mt-5 flex flex-wrap gap-5"><a href={`${sourceUrl}/docs/support-matrix.md`} className={linkStyle}>Support matrix</a><a href={`${sourceUrl}/OPEN_SOURCE_READINESS_REPORT.md`} className={linkStyle}>Readiness report</a><a href={`${sourceUrl}/SECURITY.md`} className={linkStyle}>Security policy</a></div>
            </div>
          </div>
        </section>

        <section id="license" className={sectionStyle}>
          <div className={`${containerStyle} grid grid-cols-1 gap-6 md:grid-cols-[15rem_minmax(0,1fr)] md:gap-10`}>
            <h2 className="text-2xl font-semibold tracking-tight">License</h2>
            <div className="max-w-3xl text-sm leading-7 text-slate-600"><p>The project is licensed under <a href={`${sourceUrl}/LICENSE`} className={linkStyle}>AGPL-3.0-only</a>. Commercial use is permitted. The license includes corresponding-source requirements, including for certain modified versions used over a network. The official license text governs.</p><p className="mt-4">Third-party components retain their own terms. The UI font is Manrope under the SIL Open Font License 1.1; its copyright and license notices are available below.</p><a href={`${sourceUrl}/THIRD_PARTY_NOTICES.md`} className={`mt-4 inline-block ${linkStyle}`}>Third-party notices</a></div>
          </div>
        </section>
      </main>
      <footer className="border-t border-[#ced8e6] bg-white py-7">
        <div className={`${containerStyle} flex flex-wrap items-center justify-between gap-5 text-sm text-slate-600`}>
          <p className="font-medium text-slate-900">Master Builder</p>
          <nav aria-label="Footer navigation" className="flex flex-wrap gap-x-5 gap-y-3"><a href={repositoryUrl} className={linkStyle}>GitHub</a><Link href={accountHref} className={linkStyle}>{accountLabel}</Link><Link href="/privacy" className={linkStyle}>Privacy</Link><a href="/licenses/manrope-OFL.txt" className={linkStyle}>Font license</a><a href="/licenses/manrope-NOTICE.txt" className={linkStyle}>Font notice</a></nav>
        </div>
      </footer>
    </div>
  );
}
