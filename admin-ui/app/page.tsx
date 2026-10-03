import Link from "next/link";
import { cookies } from "next/headers";
import type { Metadata } from "next";
import { ArrowRight, Blocks, ClipboardCheck, GitPullRequest, Layers3, ListChecks, LockKeyhole, Settings2, Workflow } from "lucide-react";

import { auth } from "@/auth";
import { blogPosts } from "@/app/blog/posts";
import { GitHubStars } from "@/components/landing/github-stars";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { getLastWorkspaceCookieName } from "@/lib/workspace-preference";
import { REPOSITORY_URL as repositoryUrl } from "@/lib/public-repository";


const sourceUrl = `${repositoryUrl}/blob/main`;
const buttonStyle = "inline-flex items-center justify-center gap-2 rounded-lg bg-[#203c2b] px-5 py-3 text-sm font-semibold text-white transition hover:bg-[#315c43] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#315c43]";
const linkStyle = "rounded-sm font-semibold underline decoration-[#a1b6a7] underline-offset-4 hover:text-[#315c43] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#315c43]";
const containerStyle = "mx-auto w-full max-w-7xl px-5 sm:px-8 lg:px-12";

export const metadata: Metadata = {
  title: "Master Builder — open-source AI-assisted software delivery",
  description: "Coordinate Jira work, agent execution and GitHub review in a self-hosted software delivery workflow.",
};

const features = [
  { title: "Connect work to repositories", body: "Link Jira projects and GitHub repositories, then configure which work your agents may execute.", icon: GitPullRequest },
  { title: "Coordinate delivery stages", body: "Track planning, development, testing and review as explicit workflow stages with recorded outcomes.", icon: Workflow },
  { title: "Keep evidence with the work", body: "Inspect run history, decisions, test results and review feedback in the administration UI.", icon: ClipboardCheck },
  { title: "Configure agent execution", body: "Assign runtime profiles and execution workers to your delivery workflows. Authenticate your own runtime.", icon: Settings2 },
  { title: "Separate workspaces and projects", body: "Manage tenant access, repository allowlists and project policies from one administration interface.", icon: Layers3 },
  { title: "Make review a visible step", body: "Follow pull requests and workflow feedback while keeping merge decisions with your team.", icon: ListChecks },
];
const stages = [
  { title: "Connect", body: "Authorize your Jira projects, GitHub repositories and agent runtime." },
  { title: "Configure", body: "Set project policies, ready statuses and execution workers." },
  { title: "Run", body: "Queue work and follow planning, implementation, tests and review." },
  { title: "Review", body: "Inspect evidence and the pull request before deciding to merge." },
];

export default async function HomePage() {
  const session = await auth();
  const cookieStore = await cookies();
  const preferredTenantId = cookieStore.get(getLastWorkspaceCookieName())?.value ?? null;
  const authenticatedHref = getDefaultAuthenticatedRoute(session?.user?.principal, { preferredTenantId });
  const accountHref = session ? authenticatedHref : "/login";
  const accountLabel = session ? "Open dashboard" : "Sign in";

  return (
    <div className="min-h-screen bg-[#f8faf7] text-[#192c21]">
      <a href="#main-content" className="sr-only z-[60] rounded-lg bg-[#203c2b] px-5 py-3 text-white focus:not-sr-only focus:fixed focus:left-4 focus:top-4">Skip to content</a>
      <header className="border-b border-[#dce4db] bg-[#f8faf7]">
        <div className={`${containerStyle} flex flex-wrap items-center justify-between gap-4 py-5`}>
          <Link href="/" className={`inline-flex items-center gap-2 text-base ${linkStyle} no-underline`}><Blocks aria-hidden="true" className="h-5 w-5" />Master Builder</Link>
          <nav aria-label="Main navigation" className="flex flex-wrap items-center gap-5 text-sm">
            <a href="#features" className={`${linkStyle} hidden no-underline md:inline`}>Features</a>
            <a href="#workflow" className={`${linkStyle} hidden no-underline md:inline`}>How it works</a>
            <Link href={accountHref} className={`${linkStyle} no-underline`}>{accountLabel}</Link>
            <GitHubStars />
          </nav>
        </div>
      </header>

      <main id="main-content" tabIndex={-1}>
        <section className="border-b border-[#dce4db] bg-[radial-gradient(ellipse_at_top_right,_#e8f1e3,_transparent_65%)] py-16 sm:py-24">
          <div className={`${containerStyle} grid items-center gap-14 lg:grid-cols-[1.3fr_0.7fr]`}>
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#42634b]">Open-source software delivery</p>
              <h1 className="mt-6 text-[2.75rem] font-semibold leading-[1.09] tracking-tight sm:text-6xl lg:text-[4.5rem]">AI-assisted delivery. A workflow you can inspect.</h1>
              <p className="mt-7 max-w-2xl text-lg leading-8 text-[#526454]">Connect Jira work to agent execution and GitHub review. Master Builder gives your team a shared place to configure delivery workflows, follow progress and inspect the evidence.</p>
              <div className="mt-9 flex flex-wrap items-center gap-5">
                <a href={repositoryUrl} className={buttonStyle}>Get Started<ArrowRight aria-hidden="true" className="h-4 w-4" /></a>
                <a href="#features" className={`text-sm ${linkStyle}`}>Explore the features</a>
              </div>
              <p className="mt-6 text-sm text-[#526454]">Self-hosted · Jira + GitHub · GNU AGPLv3</p>
            </div>
            <div className="rounded-2xl border border-[#cad8c6] bg-white p-6 shadow-[0_16px_60px_-35px_#203c2b] sm:p-8">
              <p className="text-xs font-semibold uppercase tracking-widest text-[#526454]">The delivery loop</p>
              <ol className="mt-6 space-y-0">
                {["A ready work item", "Agent planning & development", "Tests & review evidence", "A pull request for your team"].map((step, index) => (
                  <li key={step} className="flex items-center gap-4 border-b border-[#e4eae1] py-5 last:border-0">
                    <span aria-hidden="true" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#edf3e9] text-xs font-semibold text-[#315c43]">{index + 1}</span>
                    <span className="text-sm font-semibold leading-6">{step}</span>
                  </li>
                ))}
              </ol>
              <p className="mt-5 text-xs leading-6 text-[#526454]">Workflow overview. Your project policy determines what runs.</p>
            </div>
          </div>
        </section>

        <section id="features" className="py-16 sm:py-24">
          <div className={containerStyle}>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#42634b]">What you can do</p>
            <h2 className="mt-4 max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">Bring the moving parts of delivery together.</h2>
            <p className="mt-5 max-w-2xl leading-8 text-[#526454]">Work items, workers, workflow state and review evidence belong in a shared system your team can inspect.</p>
            <div className="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
              {features.map(({ title, body, icon: Icon }) => (
                <article key={title} className="rounded-xl border border-[#dce4db] bg-white p-7">
                  <Icon aria-hidden="true" className="h-6 w-6 text-[#42634b]" />
                  <h3 className="mt-6 text-lg font-semibold">{title}</h3>
                  <p className="mt-3 text-sm leading-7 text-[#526454]">{body}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section id="workflow" className="border-y border-[#dce4db] bg-[#edf3e9] py-16 sm:py-24">
          <div className={containerStyle}>
            <h2 className="max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">From work item to reviewed pull request</h2>
            <p className="mt-5 max-w-2xl leading-8 text-[#526454]">Start with the tools your team already uses. Configure the workflow before handing work to an execution worker.</p>
            <ol className="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
              {stages.map((stage, index) => (
                <li key={stage.title} className="rounded-xl border border-[#cad8c6] bg-[#f8faf7] p-6">
                  <p className="text-xs font-semibold text-[#42634b]">0{index + 1}</p>
                  <h3 className="mt-4 text-xl font-semibold">{stage.title}</h3>
                  <p className="mt-3 text-sm leading-7 text-[#526454]">{stage.body}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section id="open-source" className="py-16 sm:py-24">
          <div className={`${containerStyle} grid gap-12 lg:grid-cols-2`}>
            <div>
              <LockKeyhole aria-hidden="true" className="h-7 w-7 text-[#42634b]" />
              <h2 className="mt-5 text-3xl font-semibold tracking-tight sm:text-4xl">Your infrastructure. Your source code.</h2>
              <p className="mt-5 leading-8 text-[#526454]">Run Master Builder from a full source checkout, configure your own integrations and inspect how work is handled. PostgreSQL stores operational state; the Next.js UI gives your team a view into it.</p>
              <p className="mt-5 leading-8 text-[#526454]">Licensed under <a href={`${sourceUrl}/LICENSE`} className={linkStyle}>AGPL-3.0-only</a>. Commercial use is permitted. The license includes corresponding-source requirements, including for certain modified versions used over a network. The license text governs.</p>
              <div className="mt-6 flex flex-wrap gap-5 text-sm">
                <a href={`${sourceUrl}/CONTRIBUTING.md`} className={linkStyle}>Contribute</a>
                <a href={`${sourceUrl}/docs/support-matrix.md`} className={linkStyle}>Read the support matrix</a>
              </div>
            </div>
            <div className="rounded-xl border border-[#dce4db] bg-white p-7">
              <p className="text-xs font-semibold uppercase tracking-widest text-[#42634b]">Start with the source</p>
              <pre className="mt-6 whitespace-pre-wrap break-all rounded-lg bg-[#192c21] p-5 text-xs leading-7 text-[#e8f1e3]"><code>{`git clone ${repositoryUrl}.git
cd master-builder
uv sync --frozen --extra dev
uv run --frozen --extra dev python scripts/init_local_env.py`}</code></pre>
              <p className="mt-5 text-sm leading-7 text-[#526454]">The README walks through local services, UI setup and your first health check. Running a delivery workflow also requires your own integrations and an authenticated agent runtime.</p>
              <a href={repositoryUrl} className={`mt-6 ${buttonStyle}`}>Get Started<ArrowRight aria-hidden="true" className="h-4 w-4" /></a>
            </div>
          </div>
          <aside className={`${containerStyle} mt-12`}>
            <div className="rounded-xl border border-[#dce4db] bg-[#edf3e9] p-6 text-sm leading-7 text-[#526454]">
              <p className="font-semibold text-[#192c21]">An early project, with explicit boundaries.</p>
              <p className="mt-2">Master Builder is at version 0.1.0. Public interfaces are provisional. Mobile QA, voice and provider deployment integrations are experimental. Review the support matrix and readiness report before deploying; isolate workers that execute repository code.</p>
              <a href={`${sourceUrl}/OPEN_SOURCE_READINESS_REPORT.md`} className={`mt-3 inline-block ${linkStyle}`}>Read the readiness report</a>
            </div>
          </aside>
        </section>

        <section className="border-t border-[#dce4db] bg-white py-16">
          <div className={containerStyle}>
            <div className="flex flex-wrap items-center justify-between gap-5">
              <h2 className="text-2xl font-semibold tracking-tight">Notes on AI-assisted delivery</h2>
              <Link href="/blog" className={`text-sm ${linkStyle}`}>Read the blog</Link>
            </div>
            <div className="mt-8 grid gap-6 md:grid-cols-3">
              {blogPosts.slice(0, 3).map((post) => (
                <Link key={post.slug} href={`/blog/${post.slug}`} className="rounded-lg border border-[#dce4db] p-6 transition hover:bg-[#f8faf7] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#315c43]">
                  <h3 className="text-lg font-semibold leading-7">{post.title}</h3>
                  <p className="mt-3 text-sm leading-7 text-[#526454]">{post.summary}</p>
                  <span className="mt-5 inline-flex items-center gap-2 text-sm font-semibold text-[#42634b]">Read note<ArrowRight aria-hidden="true" className="h-4 w-4" /></span>
                </Link>
              ))}
            </div>
          </div>
        </section>
      </main>
      <footer className="border-t border-[#dce4db] py-8">
        <div className={`${containerStyle} flex flex-wrap items-center justify-between gap-6 text-sm text-[#526454]`}>
          <p className="font-semibold text-[#192c21]">Master Builder</p>
          <nav aria-label="Footer navigation" className="flex flex-wrap gap-6">
            <a href={repositoryUrl} className={linkStyle}>GitHub</a>
            <Link href={accountHref} className={linkStyle}>{accountLabel}</Link>
            <Link href="/privacy" className={linkStyle}>Privacy</Link>
            <a href="/licenses/manrope-OFL.txt" className={linkStyle}>Font license</a>
            <a href="/licenses/manrope-NOTICE.txt" className={linkStyle}>Font notice</a>
          </nav>
        </div>
      </footer>
    </div>
  );
}
