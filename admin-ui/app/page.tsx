import Link from "next/link";
import { cookies } from "next/headers";
import {
  ArrowRight,
  BadgeCheck,
  ClipboardCheck,
  FileQuestion,
  Landmark,
  LockKeyhole,
  Scale,
  ShieldCheck,
  Workflow,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { auth } from "@/auth";
import { blogPosts } from "@/app/blog/posts";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { getLastWorkspaceCookieName } from "@/lib/workspace-preference";

type IconItem = {
  title: string;
  body: string;
  icon: LucideIcon;
};

const breakpoints = [
  "Product intent gets lost in prompts",
  "Context lives in private agent sessions",
  "Tickets are interpreted differently",
  "Review bottlenecks increase",
  "Teams generate more code than they can validate",
  "Standards become inconsistent",
  "Decisions and assumptions become difficult to trace",
  "Jira no longer reflects the real reasoning behind the implementation",
];

const ambiguityCosts = [
  "Wrong scope is built faster",
  "Reviewers reverse-engineer intent from code",
  "Product teams lose visibility over assumptions",
  "Engineers correct decisions that should have been clarified upfront",
  "Audit trails miss key reasoning",
];

const solutionPoints: IconItem[] = [
  {
    title: "Clarify product intent before execution",
    body: "Turn requests into shared, reviewable delivery intent before agents begin implementation.",
    icon: FileQuestion,
  },
  {
    title: "Run delivery through governed stages",
    body: "Move work through planning, implementation, test, evidence, review, and approval gates.",
    icon: Workflow,
  },
  {
    title: "Capture evidence for every handoff",
    body: "Keep decisions, assumptions, outputs, tests, and approvals connected to Jira and GitHub.",
    icon: ClipboardCheck,
  },
  {
    title: "Keep humans in control",
    body: "Use AI to increase delivery capacity while preserving engineering judgement and merge accountability.",
    icon: ShieldCheck,
  },
];

const workflowStages = [
  "Product request",
  "Clarification",
  "Jira work",
  "Planning stage",
  "Agent development",
  "Test stage",
  "Review evidence",
  "GitHub PR",
  "Human approval",
];

const evidenceQuestions = [
  "What product intent did the agent work from?",
  "What context and standards were applied?",
  "What assumptions were made?",
  "What tests and checks ran?",
  "Who reviewed and approved the work?",
  "Why is this safe to merge?",
];

const audiences: IconItem[] = [
  {
    title: "CTOs and engineering leaders",
    body: "Need confidence that AI-assisted delivery can scale without weakening quality, review, or accountability.",
    icon: Landmark,
  },
  {
    title: "Product teams",
    body: "Need unresolved intent clarified before AI-generated implementation turns ambiguity into code.",
    icon: FileQuestion,
  },
  {
    title: "Engineering teams",
    body: "Need structured workflows, reviewable outputs, and fewer private agent sessions to supervise.",
    icon: BadgeCheck,
  },
  {
    title: "Platform teams",
    body: "Need standards, auditability, and operating controls across AI-assisted engineering workflows.",
    icon: LockKeyhole,
  },
];

const comparisonRows = [
  {
    category: "Coding copilots",
    role: "Help individuals write code faster.",
    limit: "They do not create shared delivery control.",
  },
  {
    category: "Agent platforms",
    role: "Help teams run agents.",
    limit: "They do not make software delivery accountable by default.",
  },
  {
    category: "Master Builder",
    role: "Helps organisations govern AI software delivery.",
    limit: "It connects intent, execution, evidence, review, and approval.",
  },
];

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <p className="text-xs font-semibold uppercase text-[#4d6b5f]">{children}</p>;
}

function DeliveryMap() {
  return (
    <div className="pointer-events-none absolute inset-y-0 -right-28 hidden w-[52%] overflow-hidden opacity-80 lg:block">
      <div className="absolute inset-0 bg-[linear-gradient(90deg,rgba(244,246,242,0)_0%,rgba(244,246,242,0.82)_35%,rgba(244,246,242,1)_100%)]" />
      <div className="absolute right-12 top-28 w-[720px] border-l border-[#b7c1b8]">
        {["Intent", "Delivery", "Approval"].map((lane, laneIndex) => (
          <div key={lane} className="relative flex min-h-32 border-t border-[#b7c1b8]">
            <span className="absolute -left-3 top-5 h-6 w-6 rounded-full border border-[#6f8d7e] bg-[#f4f6f2]" />
            <p className="w-28 px-5 py-5 text-xs font-semibold uppercase text-[#50605a]">{lane}</p>
            <div className="grid flex-1 grid-cols-3 gap-px bg-[#c8d0c8]">
              {Array.from({ length: 3 }).map((_, index) => {
                const active = laneIndex === 0 ? index < 2 : laneIndex === 1 ? index !== 0 : index === 2;
                return (
                  <div key={`${lane}-${index}`} className="bg-[#eef1ec] p-5">
                    <div className={`h-2 w-24 ${active ? "bg-[#587766]" : "bg-[#d8ded8]"}`} />
                    <div className={`mt-4 h-2 w-36 ${active ? "bg-[#a88748]" : "bg-[#d8ded8]"}`} />
                    <div className="mt-8 flex items-center gap-2">
                      <span className={`h-6 w-6 rounded-full ${active ? "bg-[#17221f]" : "bg-[#cfd7d0]"}`} />
                      <span className={`h-px flex-1 ${active ? "bg-[#17221f]" : "bg-[#cfd7d0]"}`} />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default async function HomePage() {
  const session = await auth();
  const cookieStore = await cookies();
  const preferredTenantId = cookieStore.get(getLastWorkspaceCookieName())?.value ?? null;
  const authenticatedHref = getDefaultAuthenticatedRoute(session?.user?.principal, { preferredTenantId });
  const primaryHref = session ? authenticatedHref : "/login";

  return (
    <main className="min-h-screen overflow-x-hidden bg-[#f4f6f2] text-[#17221f]">
      <header className="sticky top-0 z-50 border-b border-[#d8ded8] bg-[#f4f6f2]/92 backdrop-blur">
        <div className="mx-auto flex w-full max-w-[1440px] items-center justify-between px-5 py-4 sm:px-8 lg:px-12">
          <Link href="/" className="text-base font-semibold text-[#17221f]">
            Master Builder
          </Link>
          <nav className="hidden items-center gap-7 text-sm font-medium text-[#52625b] md:flex">
            <a href="#problem" className="transition-colors hover:text-[#17221f]">
              Problem
            </a>
            <a href="#platform" className="transition-colors hover:text-[#17221f]">
              Platform
            </a>
            <a href="#governance" className="transition-colors hover:text-[#17221f]">
              Governance
            </a>
            <Link href="/blog" className="transition-colors hover:text-[#17221f]">
              Blog
            </Link>
          </nav>
          <div className="flex items-center gap-3">
            <Link href={primaryHref} className="hidden text-sm font-medium text-[#52625b] transition-colors hover:text-[#17221f] sm:inline-flex">
              {session ? "Open dashboard" : "Sign in"}
            </Link>
            <a
              href="#book-demo"
              className="inline-flex items-center gap-2 rounded-md bg-[#17221f] px-4 py-2 text-sm font-semibold text-white transition hover:bg-[#2f4139]"
            >
              Book a demo
              <ArrowRight className="h-4 w-4" />
            </a>
          </div>
        </div>
      </header>

      <section className="relative isolate overflow-hidden border-b border-[#d8ded8]">
        <DeliveryMap />
        <div className="relative mx-auto flex w-full max-w-[1440px] flex-col justify-center px-5 py-16 sm:px-8 sm:py-20 lg:px-12 lg:py-24">
          <div className="max-w-4xl">
            <p className="text-sm font-semibold uppercase text-[#4d6b5f]">Governed AI software delivery</p>
            <h1 className="mt-6 max-w-4xl text-5xl font-semibold leading-none text-[#101816] sm:text-6xl lg:text-7xl">
              AI scales code faster than organisations scale control.
            </h1>
            <p className="mt-8 max-w-3xl text-xl leading-9 text-[#4d5c56]">
              Coding agents can generate implementation in minutes. Production software still depends on product clarity,
              shared context, review, standards, approvals, and traceability.
            </p>
            <div className="mt-10 flex flex-wrap items-center gap-4">
              <a
                href="#book-demo"
                className="inline-flex items-center gap-2 rounded-md bg-[#17221f] px-5 py-3 text-sm font-semibold text-white transition hover:bg-[#2f4139]"
              >
                Book a demo
                <ArrowRight className="h-4 w-4" />
              </a>
              <a
                href="#workflow"
                className="inline-flex items-center gap-2 rounded-md border border-[#9aa89d] px-5 py-3 text-sm font-semibold text-[#17221f] transition hover:border-[#17221f]"
              >
                See how governed delivery works
              </a>
            </div>
          </div>
          <div className="mt-14 hidden max-w-5xl border-y border-[#c7d0c8] md:grid md:grid-cols-3">
            {[
              ["Product intent", "Clarified before implementation starts."],
              ["Review control", "Evidence moves with every stage."],
              ["Human approval", "Teams decide what reaches production."],
            ].map(([title, body]) => (
              <div key={title} className="border-[#c7d0c8] py-5 pr-8 md:border-r md:pl-6 first:pl-0 last:border-r-0">
                <p className="font-semibold text-[#17221f]">{title}</p>
                <p className="mt-2 text-sm leading-6 text-[#5d6b65]">{body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="problem" className="border-b border-[#d8ded8] bg-white py-24">
        <div className="mx-auto grid w-full max-w-[1440px] gap-16 px-5 sm:px-8 lg:grid-cols-[0.9fr_1.1fr] lg:px-12">
          <div>
            <SectionLabel>What breaks at team scale</SectionLabel>
            <h2 className="mt-5 text-4xl font-semibold leading-tight text-[#17221f] sm:text-5xl">
              AI coding works for individuals. Enterprises need governed delivery.
            </h2>
            <p className="mt-6 text-lg leading-8 text-[#52625b]">
              The problem is no longer whether AI can write code. The problem is whether an organisation can trust,
              govern, review, and coordinate AI-generated software delivery across teams.
            </p>
          </div>
          <div className="grid gap-px bg-[#d8ded8] md:grid-cols-2">
            {breakpoints.map((item) => (
              <div key={item} className="bg-[#fbfcfa] p-6 transition-colors hover:bg-[#eef1ec]">
                <p className="text-base font-medium leading-7 text-[#17221f]">{item}</p>
              </div>
            ))}
          </div>
        </div>
        <div className="mx-auto mt-16 w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <p className="border-l-4 border-[#a88748] pl-5 text-2xl font-semibold leading-9 text-[#17221f]">
            The team gains code generation, but loses shared control.
          </p>
        </div>
      </section>

      <section className="border-b border-[#d8ded8] py-24">
        <div className="mx-auto grid w-full max-w-[1440px] gap-14 px-5 sm:px-8 lg:grid-cols-2 lg:px-12">
          <div>
            <SectionLabel>The shift</SectionLabel>
            <h2 className="mt-5 text-4xl font-semibold leading-tight sm:text-5xl">
              The bottleneck is no longer writing code.
            </h2>
          </div>
          <div className="text-lg leading-8 text-[#52625b]">
            <p>
              It is validating intent, quality, and trust. Before AI, ambiguity moved at human speed:
              product managers clarified requirements, engineers asked questions, and reviewers checked fit,
              security, maintainability, and standards.
            </p>
            <p className="mt-6">
              AI compresses that loop. A vague request can become implementation before the team has agreed what
              should actually be built.
            </p>
            <div className="mt-10 grid grid-cols-2 gap-px bg-[#c7d0c8] text-base text-[#17221f] sm:grid-cols-5">
              {["More code", "More assumptions", "More review pressure", "More rework", "Less traceability"].map((item) => (
                <div key={item} className="bg-[#eef1ec] p-4">
                  {item}
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="border-b border-[#d8ded8] bg-[#17221f] py-24 text-white">
        <div className="mx-auto grid w-full max-w-[1440px] gap-16 px-5 sm:px-8 lg:grid-cols-[1fr_0.9fr] lg:px-12">
          <div>
            <p className="text-xs font-semibold uppercase text-[#b6c8bd]">Product ambiguity</p>
            <h2 className="mt-5 max-w-4xl text-4xl font-semibold leading-tight sm:text-5xl">
              AI makes unresolved product ambiguity look like progress.
            </h2>
            <p className="mt-7 max-w-3xl text-lg leading-8 text-[#d7dfd8]">
              When intent is unclear, coding agents fill in the gaps. That can create the appearance of momentum
              while pushing product decisions downstream into engineering review.
            </p>
            <p className="mt-8 text-2xl font-semibold text-[#f0cf8a]">Master Builder puts clarification before execution.</p>
          </div>
          <div className="self-end border-t border-[#596962]">
            {ambiguityCosts.map((item) => (
              <div key={item} className="border-b border-[#596962] py-5">
                <p className="text-lg leading-7 text-[#f4f6f2]">{item}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="platform" className="border-b border-[#d8ded8] bg-white py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="max-w-4xl">
            <SectionLabel>The platform</SectionLabel>
            <h2 className="mt-5 text-4xl font-semibold leading-tight text-[#17221f] sm:text-5xl">
              Master Builder turns AI coding into governed software delivery.
            </h2>
            <p className="mt-6 text-lg leading-8 text-[#52625b]">
              Master Builder gives software organisations a structured way to move from product request to reviewed
              production pull request using AI agents without losing control of the delivery process.
            </p>
          </div>
          <div className="mt-16 grid gap-px bg-[#d8ded8] md:grid-cols-2 lg:grid-cols-4">
            {solutionPoints.map(({ title, body, icon: Icon }) => (
              <div key={title} className="bg-[#fbfcfa] p-6 transition-colors hover:bg-[#eef1ec]">
                <Icon className="h-6 w-6 text-[#587766]" />
                <h3 className="mt-8 text-lg font-semibold text-[#17221f]">{title}</h3>
                <p className="mt-4 text-sm leading-7 text-[#5d6b65]">{body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="workflow" className="border-b border-[#d8ded8] py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="grid gap-12 lg:grid-cols-[0.72fr_1.28fr]">
            <div>
              <SectionLabel>How governed delivery works</SectionLabel>
              <h2 className="mt-5 text-4xl font-semibold leading-tight sm:text-5xl">
                Same delivery process. More control.
              </h2>
              <p className="mt-6 text-lg leading-8 text-[#52625b]">
                Master Builder does not replace Jira, GitHub, or engineering judgement. It adds the governed workflow
                layer between product intent and AI execution.
              </p>
            </div>
            <div className="overflow-x-auto">
              <div className="grid min-w-[860px] grid-cols-9 gap-px bg-[#bfc9c1]">
                {workflowStages.map((stage, index) => (
                  <div key={stage} className="group bg-white p-4 transition-colors hover:bg-[#eef1ec]">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold text-[#6b7a73]">{String(index + 1).padStart(2, "0")}</span>
                      {index < workflowStages.length - 1 ? <ArrowRight className="h-4 w-4 text-[#8b9a91]" /> : <Scale className="h-4 w-4 text-[#a88748]" />}
                    </div>
                    <p className="mt-10 min-h-14 text-sm font-semibold leading-6 text-[#17221f]">{stage}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="governance" className="border-b border-[#d8ded8] bg-white py-24">
        <div className="mx-auto grid w-full max-w-[1440px] gap-16 px-5 sm:px-8 lg:grid-cols-[0.9fr_1.1fr] lg:px-12">
          <div>
            <SectionLabel>Trust and governance</SectionLabel>
            <h2 className="mt-5 text-4xl font-semibold leading-tight text-[#17221f] sm:text-5xl">
              Built for teams that need to trust the output.
            </h2>
            <p className="mt-6 text-lg leading-8 text-[#52625b]">
              Enterprise software teams need answers before AI-generated code reaches production. Master Builder
              keeps those answers attached to the work.
            </p>
            <p className="mt-8 text-2xl font-semibold text-[#17221f]">AI delivery needs evidence, not just output.</p>
          </div>
          <div className="grid gap-px bg-[#d8ded8] md:grid-cols-2">
            {evidenceQuestions.map((question) => (
              <div key={question} className="bg-[#fbfcfa] p-6">
                <ShieldCheck className="h-5 w-5 text-[#587766]" />
                <p className="mt-6 text-base font-medium leading-7 text-[#17221f]">{question}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="border-b border-[#d8ded8] py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="max-w-4xl">
            <SectionLabel>Who it is for</SectionLabel>
            <h2 className="mt-5 text-4xl font-semibold leading-tight sm:text-5xl">
              For software organisations moving from AI experimentation to AI delivery.
            </h2>
          </div>
          <div className="mt-16 grid gap-px bg-[#d8ded8] md:grid-cols-2 lg:grid-cols-4">
            {audiences.map(({ title, body, icon: Icon }) => (
              <div key={title} className="bg-[#f9faf7] p-6">
                <Icon className="h-6 w-6 text-[#587766]" />
                <h3 className="mt-8 text-lg font-semibold">{title}</h3>
                <p className="mt-4 text-sm leading-7 text-[#52625b]">{body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="border-b border-[#d8ded8] bg-[#101816] py-24 text-white">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="max-w-4xl">
            <p className="text-xs font-semibold uppercase text-[#b6c8bd]">Positioning</p>
            <h2 className="mt-5 text-4xl font-semibold leading-tight sm:text-5xl">Not another coding agent.</h2>
            <p className="mt-6 text-lg leading-8 text-[#d7dfd8]">
              Plugins, agents, and coding tools increase capability. Master Builder increases organisational control.
            </p>
          </div>
          <div className="mt-14 border-t border-[#596962]">
            {comparisonRows.map((row) => (
              <div key={row.category} className="grid gap-6 border-b border-[#596962] py-7 md:grid-cols-[0.42fr_0.58fr_0.7fr]">
                <p className="text-lg font-semibold text-white">{row.category}</p>
                <p className="text-[#d7dfd8]">{row.role}</p>
                <p className="text-[#f0cf8a]">{row.limit}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="blog" className="border-b border-[#d8ded8] bg-white py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="flex flex-col justify-between gap-8 md:flex-row md:items-end">
            <div className="max-w-3xl">
              <SectionLabel>Category thinking</SectionLabel>
              <h2 className="mt-5 text-4xl font-semibold leading-tight text-[#17221f] sm:text-5xl">
                Thinking about governed AI delivery
              </h2>
              <p className="mt-6 text-lg leading-8 text-[#52625b]">
                Notes on software delivery, coding agents, review bottlenecks, product ambiguity, and the operating
                model shift from traditional engineering teams to AI-assisted delivery organisations.
              </p>
            </div>
            <Link href="/blog" className="inline-flex items-center gap-2 text-sm font-semibold text-[#17221f] underline underline-offset-4">
              Read the blog
              <ArrowRight className="h-4 w-4" />
            </Link>
          </div>
          <div className="mt-14 grid gap-px bg-[#d8ded8] md:grid-cols-3">
            {blogPosts.slice(0, 3).map((post) => (
              <Link key={post.title} href={`/blog/${post.slug}`} className="group bg-[#fbfcfa] p-6 transition-colors hover:bg-[#eef1ec]">
                <p className="text-xl font-semibold leading-8 text-[#17221f]">{post.title}</p>
                <p className="mt-5 text-sm leading-7 text-[#52625b]">{post.summary}</p>
                <span className="mt-8 inline-flex items-center gap-2 text-sm font-semibold text-[#587766]">
                  Read note
                  <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
                </span>
              </Link>
            ))}
          </div>
        </div>
      </section>

      <section id="book-demo" className="bg-[#f4f6f2] py-24">
        <div className="mx-auto grid w-full max-w-[1440px] gap-12 px-5 sm:px-8 lg:grid-cols-[1fr_0.7fr] lg:px-12">
          <div>
            <SectionLabel>Next step</SectionLabel>
            <h2 className="mt-5 max-w-4xl text-4xl font-semibold leading-tight sm:text-5xl">
              Ready to scale AI-assisted engineering without losing control?
            </h2>
            <p className="mt-6 max-w-3xl text-lg leading-8 text-[#52625b]">
              Master Builder helps software teams move from ad hoc coding-agent usage to governed delivery workflows
              built around product intent, review, traceability, and human approval.
            </p>
          </div>
          <div className="flex flex-col justify-end gap-4">
            <Link
              href="/register?intent=demo"
              className="inline-flex items-center justify-center gap-2 rounded-md bg-[#17221f] px-5 py-3 text-sm font-semibold text-white transition hover:bg-[#2f4139]"
            >
              Book a demo
              <ArrowRight className="h-4 w-4" />
            </Link>
            <Link
              href={primaryHref}
              className="inline-flex items-center justify-center rounded-md border border-[#9aa89d] px-5 py-3 text-sm font-semibold text-[#17221f] transition hover:border-[#17221f]"
            >
              {session ? "Open dashboard" : "Sign in"}
            </Link>
          </div>
        </div>
      </section>

      <footer className="border-t border-[#d8ded8] bg-[#101816] py-14 text-white">
        <div className="mx-auto grid w-full max-w-[1440px] gap-10 px-5 sm:px-8 md:grid-cols-3 lg:px-12">
          <div>
            <p className="text-base font-semibold">Master Builder</p>
            <p className="mt-4 max-w-sm text-sm leading-7 text-[#b6c8bd]">
              Governed AI software delivery for teams that need product intent, review evidence, traceability, and human approval.
            </p>
          </div>
          <div className="space-y-3">
            <p className="text-xs font-semibold uppercase text-[#d7dfd8]">Platform</p>
            <a href="#problem" className="block text-sm text-[#b6c8bd] transition hover:text-white">
              Problem
            </a>
            <a href="#workflow" className="block text-sm text-[#b6c8bd] transition hover:text-white">
              How it works
            </a>
            <Link href="/blog" className="block text-sm text-[#b6c8bd] transition hover:text-white">
              Blog
            </Link>
          </div>
          <div className="space-y-3">
            <p className="text-xs font-semibold uppercase text-[#d7dfd8]">Account</p>
            <Link href={primaryHref} className="block text-sm text-[#b6c8bd] transition hover:text-white">
              {session ? "Open dashboard" : "Sign in"}
            </Link>
            <Link href="/privacy" className="block text-sm text-[#b6c8bd] transition hover:text-white">
              Privacy policy
            </Link>
          </div>
        </div>
      </footer>
    </main>
  );
}
