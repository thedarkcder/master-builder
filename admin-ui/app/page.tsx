import Image from "next/image";
import Link from "next/link";
import { cookies } from "next/headers";

import { auth } from "@/auth";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { getLastWorkspaceCookieName } from "@/lib/workspace-preference";

const heroFluidImage =
  "https://lh3.googleusercontent.com/aida-public/AB6AXuBuvZ27cHqaDDOEJ11nIggfen9hZf4ZT5UBIR0Ucb-RxSvah0Z8DhQTirK1xQ3fg3s5uRfYrFmCHy9XiKhZxF2sR0WJ17PkNpkycMosB0qv5i9_QW60RvzAdcDdlMFoyP0MYwdDYL0wW3Bx29DElub0VobAmgHCmrC2ejoKFokjILlAkYVV-6TYoy1q2ebKcMZNP0XhOWgKGXO8RzTrqhMNF5rgCYtbM8ZvxQYRdVEi-TqrWqMFCPVczGap2xeSHLieya711aEE-2Ew";
const governancePortrait1 =
  "https://lh3.googleusercontent.com/aida-public/AB6AXuAyS1njHMhNFk-caBvT39iLsvkO9IVnoVZsg-tOi01Yo_LXn9e9xAWNUpgIWO3PV6zTO3mPtelif0KjK39RtRrrm7xOt9RJ3SYOXXDNAyQENCZji0IxFxL3spfHV7k6mbvPzkW_-AooqNJlvib2sEE_1C5VAV_167VeDPmFUnVyXqFK39BGKoUSgKOSzoj4nBGGB5Yp7vTn_goApcXrXQdPmFyNxxK_UYpqJJrnOZBDGpLRA530xiOzI5PCDJvFZc8fCJ0QKf5GovYH";
const governancePortrait2 =
  "https://lh3.googleusercontent.com/aida-public/AB6AXuC88qAyBs_pPactIy_0Ej9Dc3hO9loDLmMP6EO9sbuAjEZRehiXh-vca6ekLgmBARQjrBAqyDV3snKwhh88kVKeBM9aiRfj1YxGaxJDGWdW1XtbaCpDN-KVCGFGmVyav9nykkQEXgDwue5nZN1LAOpy32FGj1YFy4TCmLQLNT4A1cuKAQjHpg1_XB9kQloQsHOsYI7FePh_KeXz1itLlfAdX6_tLKwuemL0_MABvftvctLQfQfBj8JndjvGjkuirJnOkqTGgeHCBNTE";

const lifecycleSteps = [
  {
    number: "01",
    title: "Define your agents",
    body: "Configure agent runtimes, connect models, and set up the tools each agent can access — GitHub, Discord, Jira, and more.",
  },
  {
    number: "02",
    title: "Build workflows",
    body: "Chain agents into multi-stage pipelines with decision gates, human-in-the-loop approvals, and automatic retries.",
  },
  {
    number: "03",
    title: "Deploy and run",
    body: "Trigger runs via webhooks, Discord commands, or the dashboard. Monitor every stage in real time with full execution logs.",
  },
  {
    number: "04",
    title: "Observe and iterate",
    body: "Track token usage, stage timing, and cost breakdowns. Diagnose failures fast and tighten your pipelines over time.",
  },
];

const governancePoints = [
  {
    title: "Full visibility",
    body: "Every agent action, tool call, and decision is logged. See exactly what your agents did and why.",
  },
  {
    title: "Human-in-the-loop",
    body: "Decision gates pause execution for human review before agents proceed on critical paths.",
  },
];

const supportedRuntimes = [
  { name: "Claude Code", description: "Anthropic's agentic coding runtime with tool use, file editing, and multi-step reasoning." },
  { name: "OpenAI Codex", description: "OpenAI's cloud-based agent that runs autonomously in a sandboxed environment." },
  { name: "LM Studio", description: "Run local open-weight models as agent runtimes — fully private, zero API costs." },
  { name: "Custom Runtime", description: "Bring your own agent binary or Docker image. Any process that speaks the runtime protocol works." },
];

const selfHostedPoints = [
  {
    title: "Your infrastructure, your data",
    body: "Run Master Builder on your own servers, VPC, or air-gapped environment. Nothing leaves your network.",
  },
  {
    title: "Docker Compose or Kubernetes",
    body: "Ship as a single docker-compose stack for small teams, or deploy on Kubernetes with Helm charts for production scale.",
  },
  {
    title: "No vendor lock-in",
    body: "Swap runtimes freely — move from Claude to a local LM Studio model without changing your workflows.",
  },
];

export default async function HomePage() {
  const session = await auth();
  const cookieStore = await cookies();
  const preferredTenantId = cookieStore.get(getLastWorkspaceCookieName())?.value ?? null;
  const primaryHref = session
    ? getDefaultAuthenticatedRoute(session.user?.principal, { preferredTenantId })
    : "/login";
  const primaryLabel = session ? "Open dashboard" : "Get started";

  return (
    <main className="min-h-screen overflow-x-hidden bg-[#111318] text-[#e2e2e8]">
      <header className="sticky top-0 z-50 border-b border-white/[0.06] bg-[#111318]/85 backdrop-blur-xl">
        <div className="mx-auto flex w-full max-w-[1440px] items-center justify-between px-5 py-5 sm:px-8 lg:px-12">
          <Link href="/" className="text-sm font-bold tracking-tight text-white sm:text-base">
            Master Builder
          </Link>
          <nav className="hidden items-center gap-8 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-400 md:flex">
            <a href="#how-it-works" className="text-[#78d1ff]">
              How it works
            </a>
            <Link href="/tutorials" className="transition-colors hover:text-white">
              Tutorials
            </Link>
            <a href="#runtimes" className="transition-colors hover:text-white">
              Runtimes
            </a>
            <a href="#observability" className="transition-colors hover:text-white">
              Observability
            </a>
            <a href="#self-hosted" className="transition-colors hover:text-white">
              Self-hosted
            </a>
          </nav>
          <div className="flex items-center gap-3">
            <Link
              href="/register"
              className="hidden rounded-md bg-[#282a2e] px-4 py-2 text-xs font-semibold tracking-tight text-white transition hover:bg-[#37393e] sm:inline-flex"
            >
              Create workspace
            </Link>
            <Link
              href={primaryHref}
              className="rounded-md bg-[linear-gradient(135deg,#78d1ff_0%,#0b9bcf_100%)] px-4 py-2 text-xs font-bold tracking-tight text-[#002d40] transition hover:shadow-[0_0_28px_rgba(120,209,255,0.2)]"
            >
              {primaryLabel}
            </Link>
          </div>
        </div>
      </header>

      <section className="relative isolate w-full overflow-hidden">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_50%_-20%,rgba(120,209,255,0.12),transparent)]" />
        <div className="relative mx-auto flex min-h-[calc(100svh-5.25rem)] w-full max-w-[1440px] items-center px-5 py-14 sm:px-8 sm:py-16 lg:px-12 lg:py-20">
          <div className="grid w-full items-center gap-12 lg:grid-cols-[minmax(0,1.08fr)_minmax(320px,0.92fr)] lg:gap-10 xl:gap-14">
          <div className="relative z-10 max-w-3xl">
            <p className="text-[11px] font-bold uppercase tracking-[0.28em] text-[#78d1ff]">Agent orchestration platform</p>
            <h1 className="mt-4 max-w-[14ch] text-[2.75rem] font-extrabold leading-[0.95] tracking-[-0.04em] text-white sm:text-[3.75rem] lg:text-[4.5rem]">
              Master Builder
            </h1>
            <p className="mt-5 max-w-2xl text-xl font-light leading-snug text-[#e8edf2] sm:text-2xl sm:leading-snug">
              Deploy, manage, and observe{" "}
              <span className="font-semibold text-[#78d1ff]">AI agent teams.</span>
            </p>
            <p className="mt-6 max-w-2xl text-base font-light leading-relaxed text-[#bdc8d0] sm:text-lg sm:leading-8">
              Build multi-agent workflows that connect to GitHub, Discord, and Jira.
              Run them on demand or via webhooks, with full observability into every stage, tool call, and token spent.
            </p>
            <div className="mt-10 flex flex-wrap items-center gap-4">
              <Link
                href={primaryHref}
                className="rounded-md bg-[linear-gradient(135deg,#78d1ff_0%,#0b9bcf_100%)] px-6 py-3.5 text-sm font-extrabold tracking-tight text-[#002d40] transition hover:scale-[1.01] hover:shadow-[0_0_30px_rgba(120,209,255,0.22)]"
              >
                {primaryLabel}
              </Link>
              <Link
                href="/register"
                className="rounded-md bg-[#333539] px-6 py-3.5 text-sm font-semibold tracking-tight text-white transition hover:bg-[#37393e]"
              >
                Create workspace
              </Link>
            </div>
          </div>

          <div className="relative mx-auto flex min-h-[360px] w-full max-w-[560px] items-center justify-center lg:mx-0 lg:min-h-[520px] lg:max-w-none lg:justify-end xl:min-h-[580px]">
            <div className="pointer-events-none absolute inset-[-12%] rounded-[28px] bg-[radial-gradient(circle_at_25%_30%,rgba(120,209,255,0.2),transparent_35%),radial-gradient(circle_at_72%_68%,rgba(11,155,207,0.14),transparent_38%)] blur-2xl" />
            <div className="relative aspect-[4/5] w-full max-w-[420px] overflow-hidden rounded-[30px] bg-[#15171c] shadow-[0_40px_120px_-40px_rgba(0,0,0,0.85)] sm:max-w-[460px] lg:max-w-[min(560px,92%)]">
              <Image
                src={heroFluidImage}
                alt="Abstract fluid render representing multi-agent orchestration."
                fill
                priority
                sizes="(max-width: 1024px) 90vw, 40vw"
                className="object-cover opacity-70 mix-blend-screen"
              />
            </div>
          </div>
        </div>
        </div>
      </section>

      <section id="how-it-works" className="bg-[#1a1c20] py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="mb-16 text-center">
            <p className="text-[11px] font-bold uppercase tracking-[0.24em] text-[#78d1ff]">How it works</p>
            <h2 className="mt-4 text-3xl font-bold tracking-[-0.03em] text-white sm:text-4xl">
              From agent config to production pipeline in minutes.
            </h2>
          </div>
          <div className="grid gap-px bg-[#111318] md:grid-cols-2 xl:grid-cols-4">
            {lifecycleSteps.map((step) => (
              <div key={step.number} className="bg-[#15171c] p-8 transition-colors duration-300 hover:bg-[#1e2024]">
                <p className="text-5xl font-extralight text-[#333539]">{step.number}</p>
                <h3 className="mt-6 text-lg font-bold uppercase tracking-[-0.03em] text-white">{step.title}</h3>
                <p className="mt-4 text-sm leading-7 text-[#bdc8d0]">{step.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="runtimes" className="py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="mb-16 text-center">
            <p className="text-[11px] font-bold uppercase tracking-[0.24em] text-[#78d1ff]">Supported runtimes</p>
            <h2 className="mt-4 text-3xl font-bold tracking-[-0.03em] text-white sm:text-4xl">
              Works with the agents you already use.
            </h2>
            <p className="mx-auto mt-4 max-w-2xl text-base leading-relaxed text-[#bdc8d0]">
              Plug in cloud-hosted models, local open-weight runtimes, or your own custom agent binaries. Mix and match within the same workflow.
            </p>
          </div>
          <div className="grid gap-6 sm:grid-cols-2 xl:grid-cols-4">
            {supportedRuntimes.map((rt) => (
              <div key={rt.name} className="group rounded-2xl border border-white/[0.06] bg-[#15171c] p-6 transition-colors duration-300 hover:border-[#78d1ff]/30 hover:bg-[#1a1c20]">
                <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-lg bg-[#78d1ff]/10">
                  <span className="text-lg font-bold text-[#78d1ff]">{rt.name.charAt(0)}</span>
                </div>
                <h3 className="text-base font-bold text-white">{rt.name}</h3>
                <p className="mt-2 text-sm leading-7 text-[#bdc8d0]">{rt.description}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="observability" className="py-24">
        <div className="mx-auto grid w-full max-w-[1440px] gap-16 px-5 sm:px-8 lg:grid-cols-[0.92fr_1.08fr] lg:px-12">
          <div className="grid grid-cols-2 gap-4 sm:gap-6">
            <div className="space-y-4 pt-8">
              <div className="relative aspect-[3/4] overflow-hidden rounded-xl bg-[#1e2024] grayscale transition duration-700 hover:grayscale-0">
                <Image
                  src={governancePortrait1}
                  alt="Agent pipeline visualization."
                  fill
                  sizes="(max-width: 768px) 50vw, 20vw"
                  className="object-cover"
                />
                <div className="absolute inset-x-0 bottom-0 bg-[linear-gradient(180deg,transparent,rgba(12,14,18,0.88))] p-4">
                  <p className="text-sm font-bold text-white">Activity timeline</p>
                  <p className="text-[11px] uppercase tracking-[0.18em] text-[#bdc8d0]">Every tool call logged</p>
                </div>
              </div>
              <div className="flex aspect-square flex-col justify-end rounded-xl bg-[#1e2024] p-6">
                <p className="text-xs uppercase tracking-[0.22em] text-[#78d1ff]">Cost tracking</p>
                <p className="mt-3 text-sm leading-7 text-[#bdc8d0]">Token usage breakdowns per agent, per stage, per run. Know exactly what your agents cost.</p>
              </div>
            </div>

            <div className="space-y-4">
              <div className="flex aspect-square flex-col justify-end rounded-xl bg-[#78d1ff] p-6 text-[#002d40]">
                <p className="text-xs font-bold uppercase tracking-[0.22em]">Decision gates</p>
                <p className="mt-3 text-2xl font-extrabold tracking-[-0.04em]">Humans stay in control of critical decisions.</p>
              </div>
              <div className="relative aspect-[3/4] overflow-hidden rounded-xl bg-[#1e2024] grayscale transition duration-700 hover:grayscale-0">
                <Image
                  src={governancePortrait2}
                  alt="Workflow pipeline stages."
                  fill
                  sizes="(max-width: 768px) 50vw, 20vw"
                  className="object-cover"
                />
                <div className="absolute inset-x-0 bottom-0 bg-[linear-gradient(180deg,transparent,rgba(12,14,18,0.88))] p-4">
                  <p className="text-sm font-bold text-white">Multi-stage pipelines</p>
                  <p className="text-[11px] uppercase tracking-[0.18em] text-[#bdc8d0]">Chain agents together</p>
                </div>
              </div>
            </div>
          </div>

          <div className="self-center">
            <p className="text-[11px] font-bold uppercase tracking-[0.24em] text-[#78d1ff]">Observability</p>
            <h2 className="mt-4 text-4xl font-extrabold leading-tight tracking-[-0.04em] text-white sm:text-5xl">
              See everything your agents do.
            </h2>
            <p className="mt-8 max-w-2xl text-lg leading-8 text-[#bdc8d0]">
              Agents are powerful but opaque without the right tooling. Master Builder gives you a live activity timeline
              of every message, tool call, command, and file edit — plus token analytics, cost tracking, and stage diagnostics.
            </p>
            <ul className="mt-10 space-y-6">
              {governancePoints.map((point) => (
                <li key={point.title} className="flex items-start gap-4">
                  <span className="mt-2 h-2 w-2 rounded-full bg-[#78d1ff]" />
                  <div>
                    <h3 className="text-base font-bold text-white">{point.title}</h3>
                    <p className="mt-2 text-sm leading-7 text-[#bdc8d0]">{point.body}</p>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <section id="self-hosted" className="bg-[#1a1c20] py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="grid items-center gap-16 lg:grid-cols-2">
            <div>
              <p className="text-[11px] font-bold uppercase tracking-[0.24em] text-[#78d1ff]">Self-hosted</p>
              <h2 className="mt-4 text-4xl font-extrabold leading-tight tracking-[-0.04em] text-white sm:text-5xl">
                Runs on your own infrastructure.
              </h2>
              <p className="mt-6 max-w-xl text-lg leading-8 text-[#bdc8d0]">
                Master Builder is fully self-hosted. Deploy it alongside your existing services —
                your code, your secrets, and your agent logs never leave your network.
              </p>
            </div>
            <ul className="space-y-6">
              {selfHostedPoints.map((point) => (
                <li key={point.title} className="rounded-2xl border border-white/[0.06] bg-[#15171c] p-6">
                  <h3 className="text-base font-bold text-white">{point.title}</h3>
                  <p className="mt-2 text-sm leading-7 text-[#bdc8d0]">{point.body}</p>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <section id="get-started" className="py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="relative overflow-hidden rounded-[28px] bg-[#1a1c20] p-8 sm:p-12 lg:p-16">
            <div className="absolute right-0 top-0 h-full w-1/3 bg-[radial-gradient(circle_at_center,rgba(120,209,255,0.16),transparent_62%)] blur-[110px]" />
            <div className="relative max-w-3xl">
              <h2 className="text-4xl font-extrabold tracking-[-0.04em] text-white sm:text-5xl">
                Start deploying agents today.
              </h2>
              <p className="mt-5 text-lg font-light leading-8 text-[#bdc8d0]">
                Create a workspace, connect your repos and integrations, then build your first
                multi-agent workflow. You can be running agents in production in under 10 minutes.
              </p>

              <div className="mt-10 flex flex-wrap gap-4">
                <Link
                  href="/register"
                  className="rounded-md bg-[linear-gradient(135deg,#78d1ff_0%,#0b9bcf_100%)] px-6 py-3.5 text-sm font-extrabold tracking-tight text-[#002d40] transition hover:scale-[1.01]"
                >
                  Create workspace
                </Link>
                <Link
                  href={primaryHref}
                  className="rounded-md bg-[#282a2e] px-6 py-3.5 text-sm font-semibold tracking-tight text-white transition hover:bg-[#333539]"
                >
                  {session ? "Open dashboard" : "Sign in"}
                </Link>
              </div>
            </div>
          </div>
        </div>
      </section>

      <footer className="bg-[#0c0e12] py-16">
        <div className="mx-auto grid w-full max-w-[1440px] gap-12 px-5 sm:px-8 md:grid-cols-3 lg:px-12">
          <div>
            <p className="text-base font-bold text-white">Master Builder</p>
            <p className="mt-4 max-w-xs text-sm leading-7 text-slate-500">
              Deploy, manage, and observe AI agent teams with multi-stage workflows, decision gates, and full execution logs.
            </p>
          </div>
          <div className="space-y-4">
            <p className="text-[11px] font-bold uppercase tracking-[0.22em] text-white">Platform</p>
            <Link href="/register" className="block text-sm text-slate-500 transition-colors hover:text-white">
              Create workspace
            </Link>
            <Link href="/tutorials" className="block text-sm text-slate-500 transition-colors hover:text-white">
              Tutorials
            </Link>
            <Link href="/privacy" className="block text-sm text-slate-500 transition-colors hover:text-white">
              Privacy policy
            </Link>
          </div>
          <div className="space-y-4">
            <p className="text-[11px] font-bold uppercase tracking-[0.22em] text-white">Account</p>
            <Link href={primaryHref} className="block text-sm text-slate-500 transition-colors hover:text-white">
              {session ? "Open dashboard" : "Sign in"}
            </Link>
            <Link href="/forgot-password" className="block text-sm text-slate-500 transition-colors hover:text-white">
              Reset password
            </Link>
          </div>
        </div>
      </footer>
    </main>
  );
}
