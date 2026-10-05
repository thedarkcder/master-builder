import Link from "next/link";
import type { Metadata } from "next";
import { ArrowRight, Blocks } from "lucide-react";

import { GitHubStars } from "@/components/landing/github-stars";
import { BASE_PATH, REPOSITORY_URL as repositoryUrl } from "@/lib/public-repository";

const sourceUrl = `${repositoryUrl}/blob/main`;
const containerStyle = "mx-auto w-full max-w-6xl px-5 sm:px-8";
const linkStyle = "rounded-sm font-medium underline decoration-slate-300 underline-offset-4 hover:text-[#245ac7] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#245ac7]";
const sectionStyle = "border-t border-slate-200 py-12 sm:py-16";
const codeStyle = "min-w-0 overflow-x-auto rounded-md border border-[#263b59] bg-[#14243b] p-5 font-mono text-xs leading-7 text-[#e2eaf5] sm:text-sm";

export const metadata: Metadata = {
  title: "Open Factory — Open-source software factory",
  description: "Open Factory is an extensible open-source software factory. Encode your engineering practices in configurable workflows, agent profiles, policies and review gates; plan, build, test and review software in your own environment.",
};

const features = [
  {
    id: "work-planning",
    title: "Work planning and human decisions",
    status: "Requires Jira and an agent runtime",
    body: "Start planning from the project board, inspect engineering tasks, and answer clarification questions in connected Jira or Discord follow-ups before work continues. Review planning progress, recorded decisions and blocked questions in the administration UI.",
    links: [{ label: "Planning and decision rules", path: "docs/run-decision-engine.md#decision-gate-contract" }],
  },
  {
    id: "workflow-execution",
    title: "Workflow execution",
    status: "Requires configured workers",
    body: "Run configured planning, development, test and review operations with selected agent profiles. Inspect each attempt, resume eligible executions, retry failed operations or start a fresh rerun through the controls available for that run.",
    links: [{ label: "Runtime requirements", path: "docs/support-matrix.md#validation-boundaries" }],
  },
  {
    id: "run-inspection",
    title: "Run inspection and token usage",
    status: "Run views and platform analytics",
    body: "Inspect run timelines, operation attempts, logs, tool calls and review evidence. Compare recorded input, cached and output token usage across runs, stages and models. Usage views depend on the telemetry recorded for each execution.",
    links: [],
  },
  {
    id: "project-access",
    title: "Projects and team access",
    status: "Administration UI and API",
    body: "Create projects, connect authorized repositories, invite members and manage teams, roles and project access requests. Configure delivery policy and managed secret references for each project, with access governed by workspace permissions.",
    links: [{ label: "Configuration guide", path: "docs/configuration.md#required-core-settings" }],
  },
  {
    id: "project-knowledge",
    title: "Project knowledge",
    status: "Platform administration",
    body: "Upload project documents, review knowledge assets and browse their extracted chunks and facts. Configure supported external sources and inspect synchronization results. Search and retrieval use the configured knowledge and embedding services.",
    links: [],
  },
  {
    id: "code-review",
    title: "Code review in GitHub",
    status: "Requires a GitHub App installation",
    body: "Connect GitHub repositories, create delivery branches and pull requests, and bring check results and review feedback back into the workflow. Repository allowlists and project policy govern which code an execution can access and how changes are reviewed.",
    links: [{ label: "GitHub App setup", path: "docs/github-app-oauth-onboarding.md#github-app-creation-checklist" }],
  },
  {
    id: "preview-qa",
    title: "Deployment previews and QA",
    status: "Experimental integration",
    body: "Configure deployment targets, inspect preview and release attempts, and collect browser or mobile QA recordings against configured builds. Review evidence through authenticated run links. Provider deployment, recording and recovery require separate toolchains and live validation.",
    links: [{ label: "Deployment and QA status", path: "docs/support-matrix.md#validation-boundaries" }],
  },
  {
    id: "discord-collaboration",
    title: "Discord collaboration",
    status: "Requires a configured Discord application",
    body: "Connect project channels, receive delivery notifications and use permission-checked commands to inspect or control work. Clarification threads let people answer questions associated with a run. Available commands depend on the workspace’s configured integrations and permissions.",
    links: [{ label: "Discord command contracts", path: "docs/run-decision-engine.md#discord-commands" }],
  },
  {
    id: "team-briefings",
    title: "Scheduled team briefings",
    status: "Experimental integration",
    body: "Schedule stand-up and retrospective voice briefings with a timezone and reporting window. Enable, disable or manually trigger the configured briefing and inspect its workflow history. Briefings are delivered as audio attachments in a Discord project channel. This requires configured text-to-speech models and separate end-to-end validation.",
    links: [{ label: "Briefing prerequisites", path: "docs/configuration.md#optional-integrations" }],
  },
];

export default function HomePage() {
  return (
    <div className="min-h-screen bg-[#f5f7fb] text-[#15243b]">
      <a href="#main-content" className="sr-only z-[60] rounded-md bg-[#245ac7] px-5 py-3 text-white focus:not-sr-only focus:fixed focus:left-4 focus:top-4">Skip to content</a>
      <header className="border-b border-[#ced8e6] bg-white">
        <div className={`${containerStyle} flex flex-wrap items-center justify-between gap-4 py-5`}>
          <Link href="/" className={`inline-flex items-center gap-3 font-mono text-sm tracking-tight ${linkStyle} no-underline`}><span className="flex h-8 w-8 items-center justify-center rounded border border-[#c8d7ee] bg-[#edf3fc] text-[#245ac7]"><Blocks aria-hidden="true" className="h-4 w-4" /></span>Open Factory</Link>
          <nav aria-label="Main navigation" className="flex flex-wrap items-center gap-4 text-sm sm:gap-5">
            <a href="#capabilities" className={`${linkStyle} no-underline`}>Features</a>
            <a href="#architecture" className={`${linkStyle} no-underline`}>How it works</a>
            <a href={`${sourceUrl}/README.md`} className={`${linkStyle} no-underline`}>Documentation</a>
            <GitHubStars />
          </nav>
        </div>
      </header>

      <main id="main-content" tabIndex={-1}>
        <section className="border-b border-[#d5deea] bg-white bg-[linear-gradient(to_right,#e7edf580_1px,transparent_1px),linear-gradient(to_bottom,#e7edf580_1px,transparent_1px)] bg-[size:28px_28px] py-12 sm:py-16">
          <div className={`${containerStyle} grid grid-cols-1 gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]`}>
            <div className="min-w-0">
              <p className="inline-block border-l-2 border-[#245ac7] bg-white px-3 py-1 font-mono text-xs text-[#4c6587]">Open Factory / technical overview</p>
              <h1 className="mt-5 max-w-3xl text-3xl font-semibold leading-tight tracking-tight sm:text-[2.75rem]">Open-source software factory</h1>
              <p className="mt-6 max-w-3xl text-base leading-8 text-slate-600">Build your own software factory: encode how your team plans, builds, tests and reviews software in workflows, agent profiles, project policies and human review gates. Run it in infrastructure you control, including your own cloud environment. Connect project knowledge, inspect execution and token usage, and extend your factory with configured delivery, QA and Discord integrations.</p>
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
            <p className="mb-3 font-mono text-xs uppercase tracking-wider text-[#4c6587]">01 / Features</p>
            <h2 className="text-2xl font-semibold tracking-tight">What’s included</h2>
            <p className="mt-4 max-w-3xl text-sm leading-7 text-slate-600">These are the user-facing capabilities included in the codebase. Delivery and external integrations require their own configuration; experimental features are marked below.</p>
            <nav aria-label="Feature navigation" className="mt-5 flex flex-wrap gap-2 text-sm">
              {features.map((item) => <a key={item.id} href={`#${item.id}`} className={`${linkStyle} border border-[#cfdaea] bg-white px-3 py-2 text-xs no-underline`}>{item.title}</a>)}
            </nav>
            <div className="mt-8 divide-y divide-slate-200">
              {features.map((item, index) => (
                <article id={item.id} key={item.id} className="grid grid-cols-1 gap-4 py-7 md:grid-cols-[15rem_minmax(0,1fr)] md:gap-10">
                  <div><p className="font-mono text-xs text-[#245ac7]">FEATURE / 0{index + 1}</p><h3 className="mt-2 text-lg font-semibold">{item.title}</h3></div>
                  <div><p className="mb-3 inline-block border border-[#cfdaea] bg-white px-2 py-1 font-mono text-[11px] text-[#4c6587]">{item.status}</p><p className="max-w-3xl text-sm leading-7 text-slate-600">{item.body}</p><div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 text-sm"><a href={`${sourceUrl}/docs/features.md#${item.id}`} className={linkStyle}>Feature details</a>{item.links.map((link) => <a key={link.path} href={`${sourceUrl}/${link.path}`} className={linkStyle}>{link.label}</a>)}</div></div>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section id="architecture" className={`${sectionStyle} bg-[#edf2f8]`}>
          <div className={containerStyle}>
            <p className="mb-3 font-mono text-xs uppercase tracking-wider text-[#4c6587]">02 / How it works</p>
            <h2 className="text-2xl font-semibold tracking-tight">From work item to reviewed change</h2>
            <p className="mt-4 max-w-3xl text-sm leading-7 text-slate-600">Connect a project to its work tracker and repositories, choose a workflow and agent runtime, and inspect the results at each step. Human questions, blocked operations and failed attempts remain available for review and action.</p>
            <ol aria-label="Delivery workflow" className="mt-7 grid grid-cols-1 gap-3 rounded-md border border-[#263b59] bg-[#14243b] p-5 text-sm text-[#e2eaf5] sm:grid-cols-5">
              {["Select project and work", "Plan and resolve questions", "Run configured agent stages", "Inspect tests and evidence", "Review the GitHub pull request"].map((step, index) => <li key={step} className="min-w-0 border-l border-[#42628a] pl-3"><span className="block font-mono text-xs text-[#9ebbf5]">0{index + 1}</span><span className="mt-2 block leading-6">{step}</span></li>)}
            </ol>
            <p className="mt-3 text-xs leading-6 text-[#4c6587]">Typical delivery journey. The configured workflow determines its operations and gates; a step can require human input or stop on failure.</p>
            <div className="mt-6 flex flex-wrap gap-5 text-sm"><a href={`${sourceUrl}/docs/features.md`} className={linkStyle}>Complete feature guide</a><a href={`${sourceUrl}/README.md#architecture`} className={linkStyle}>Source architecture</a><a href={`${sourceUrl}/docs/public-contracts.md#http-interfaces`} className={linkStyle}>HTTP interfaces</a></div>
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
              <p>Open Factory is at version 0.1.0. API schemas, workflow formats and extension interfaces are provisional. Pin a reviewed revision and rerun integration contracts on upgrades.</p>
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
          <p className="font-medium text-slate-900">Open Factory</p>
          <nav aria-label="Footer navigation" className="flex flex-wrap gap-x-5 gap-y-3"><a href={repositoryUrl} className={linkStyle}>GitHub</a><Link href="/privacy" className={linkStyle}>Privacy</Link><a href={`${BASE_PATH}/licenses/manrope-OFL.txt`} className={linkStyle}>Font license</a><a href={`${BASE_PATH}/licenses/manrope-NOTICE.txt`} className={linkStyle}>Font notice</a></nav>
        </div>
      </footer>
    </div>
  );
}
