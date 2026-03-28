import Link from "next/link";

import { auth } from "@/auth";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";

const lifecycleSteps = [
  {
    number: "01",
    title: "Strategic discovery",
    body: "Define business drivers, operating constraints, and the executive outcomes the workspace must deliver.",
  },
  {
    number: "02",
    title: "Intelligent synthesis",
    body: "Translate goals into governed operating flows across Jira, GitHub, Discord, and delivery analytics.",
  },
  {
    number: "03",
    title: "Continuous orchestration",
    body: "Run structured execution cycles with clear controls, visibility, and fast operational feedback.",
  },
  {
    number: "04",
    title: "Elite delivery",
    body: "Keep leadership oversight and technical depth aligned without forcing every team into the same view.",
  },
];

const governancePoints = [
  {
    title: "Strategic alignment",
    body: "Every workflow and permission model stays tied to measurable delivery outcomes.",
  },
  {
    title: "Refined operating experience",
    body: "Business and technical teams share one platform without sharing unnecessary complexity.",
  },
];

export default async function HomePage() {
  const session = await auth();
  const primaryHref = session ? getDefaultAuthenticatedRoute(session.user?.principal) : "/login";
  const primaryLabel = session ? "Open workspace" : "Strategic consultation";

  return (
    <main className="min-h-screen bg-[#111318] font-[Manrope] text-[#e2e2e8]">
      <header className="sticky top-0 z-50 bg-[#111318]/80 backdrop-blur-xl">
        <div className="mx-auto flex w-full max-w-[1440px] items-center justify-between px-5 py-5 sm:px-8 lg:px-12">
          <Link href="/" className="text-sm font-bold tracking-tight text-white sm:text-base">
            Master Builder
          </Link>
          <nav className="hidden items-center gap-8 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-400 md:flex">
            <a href="#methodology" className="text-[#78d1ff]">
              Methodology
            </a>
            <a href="#governance" className="transition-colors hover:text-white">
              Oversight
            </a>
            <a href="#briefing" className="transition-colors hover:text-white">
              Strategic briefing
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

      <section className="mx-auto flex min-h-[calc(100svh-84px)] w-full max-w-[1440px] items-center overflow-hidden px-5 py-16 sm:px-8 lg:px-12">
        <div className="grid w-full items-center gap-14 lg:grid-cols-[minmax(0,1.1fr)_minmax(340px,0.9fr)] lg:gap-8">
          <div className="relative z-10 max-w-3xl">
            <h1 className="max-w-3xl text-[3rem] font-extrabold leading-[0.92] tracking-[-0.04em] text-white sm:text-[4.5rem] lg:text-[5.4rem]">
              Transforming vision into <span className="text-[#78d1ff]">digital reality.</span>
            </h1>
            <p className="mt-7 max-w-2xl text-base font-light leading-8 text-[#bdc8d0] sm:text-lg">
              Accelerate business impact through intelligent partnership. Master Builder turns complex objectives into
              governed digital operations with structured execution, clear controls, and senior-level oversight.
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
                Executive portfolio
              </Link>
            </div>
          </div>

          <div className="relative min-h-[420px] lg:min-h-[620px]">
            <div className="absolute inset-0 rounded-[28px] bg-[radial-gradient(circle_at_25%_30%,rgba(120,209,255,0.18),transparent_32%),radial-gradient(circle_at_70%_70%,rgba(11,155,207,0.12),transparent_36%)] blur-2xl" />
            <div className="absolute right-[-8%] top-1/2 h-[88%] w-[92%] -translate-y-1/2 overflow-hidden rounded-[30px] bg-[#15171c]">
              <img
                src="https://lh3.googleusercontent.com/aida-public/AB6AXuBuvZ27cHqaDDOEJ11nIggfen9hZf4ZT5UBIR0Ucb-RxSvah0Z8DhQTirK1xQ3fg3s5uRfYrFmCHy9XiKhZxF2sR0WJ17PkNpkycMosB0qv5i9_QW60RvzAdcDdlMFoyP0MYwdDYL0wW3Bx29DElub0VobAmgHCmrC2ejoKFokjILlAkYVV-6TYoy1q2ebKcMZNP0XhOWgKGXO8RzTrqhMNF5rgCYtbM8ZvxQYRdVEi-TqrWqMFCPVczGap2xeSHLieya711aEE-2Ew"
                alt="Abstract fluid render representing digital growth and AI-driven transformation."
                className="h-full w-full object-cover opacity-70 mix-blend-screen"
              />
            </div>
          </div>
        </div>
      </section>

      <section id="methodology" className="bg-[#1a1c20] py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="mb-16 text-center">
            <p className="text-[11px] font-bold uppercase tracking-[0.24em] text-[#78d1ff]">The advisory lifecycle</p>
            <h2 className="mt-4 text-3xl font-bold tracking-[-0.03em] text-white sm:text-4xl">
              Precision engineering for business outcomes.
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

      <section id="governance" className="py-24">
        <div className="mx-auto grid w-full max-w-[1440px] gap-16 px-5 sm:px-8 lg:grid-cols-[0.92fr_1.08fr] lg:px-12">
          <div className="grid grid-cols-2 gap-4 sm:gap-6">
            <div className="space-y-4 pt-8">
              <div className="relative aspect-[3/4] overflow-hidden rounded-xl bg-[#1e2024] grayscale transition duration-700 hover:grayscale-0">
                <img
                  src="https://lh3.googleusercontent.com/aida-public/AB6AXuAyS1njHMhNFk-caBvT39iLsvkO9IVnoVZsg-tOi01Yo_LXn9e9xAWNUpgIWO3PV6zTO3mPtelif0KjK39RtRrrm7xOt9RJ3SYOXXDNAyQENCZji0IxFxL3spfHV7k6mbvPzkW_-AooqNJlvib2sEE_1C5VAV_167VeDPmFUnVyXqFK39BGKoUSgKOSzoj4nBGGB5Yp7vTn_goApcXrXQdPmFyNxxK_UYpqJJrnOZBDGpLRA530xiOzI5PCDJvFZc8fCJ0QKf5GovYH"
                  alt="Executive advisor portrait."
                  className="h-full w-full object-cover"
                />
                <div className="absolute inset-x-0 bottom-0 bg-[linear-gradient(180deg,transparent,rgba(12,14,18,0.88))] p-4">
                  <p className="text-sm font-bold text-white">Marcus Chen</p>
                  <p className="text-[11px] uppercase tracking-[0.18em] text-[#bdc8d0]">Director of excellence</p>
                </div>
              </div>
              <div className="flex aspect-square flex-col justify-end rounded-xl bg-[#1e2024] p-6">
                <p className="text-xs uppercase tracking-[0.22em] text-[#78d1ff]">Uncompromising quality</p>
                <p className="mt-3 text-sm leading-7 text-[#bdc8d0]">Every workspace rollout is reviewed through delivery quality, governance, and executive clarity.</p>
              </div>
            </div>

            <div className="space-y-4">
              <div className="flex aspect-square flex-col justify-end rounded-xl bg-[#78d1ff] p-6 text-[#002d40]">
                <p className="text-xs font-bold uppercase tracking-[0.22em]">Executive advisory layer</p>
                <p className="mt-3 text-2xl font-extrabold tracking-[-0.04em]">Human oversight with AI execution.</p>
              </div>
              <div className="relative aspect-[3/4] overflow-hidden rounded-xl bg-[#1e2024] grayscale transition duration-700 hover:grayscale-0">
                <img
                  src="https://lh3.googleusercontent.com/aida-public/AB6AXuC88qAyBs_pPactIy_0Ej9Dc3hO9loDLmMP6EO9sbuAjEZRehiXh-vca6ekLgmBARQjrBAqyDV3snKwhh88kVKeBM9aiRfj1YxGaxJDGWdW1XtbaCpDN-KVCGFGmVyav9nykkQEXgDwue5nZN1LAOpy32FGj1YFy4TCmLQLNT4A1cuKAQjHpg1_XB9kQloQsHOsYI7FePh_KeXz1itLlfAdX6_tLKwuemL0_MABvftvctLQfQfBj8JndjvGjkuirJnOkqTGgeHCBNTE"
                  alt="Chief product strategist portrait."
                  className="h-full w-full object-cover"
                />
                <div className="absolute inset-x-0 bottom-0 bg-[linear-gradient(180deg,transparent,rgba(12,14,18,0.88))] p-4">
                  <p className="text-sm font-bold text-white">Sarah Voss</p>
                  <p className="text-[11px] uppercase tracking-[0.18em] text-[#bdc8d0]">Chief product strategist</p>
                </div>
              </div>
            </div>
          </div>

          <div className="self-center">
            <p className="text-[11px] font-bold uppercase tracking-[0.24em] text-[#78d1ff]">Premium governance</p>
            <h2 className="mt-4 text-4xl font-extrabold leading-tight tracking-[-0.04em] text-white sm:text-5xl">
              Computational efficiency. Expert judgment.
            </h2>
            <p className="mt-8 max-w-2xl text-lg leading-8 text-[#bdc8d0]">
              Intelligent execution accelerates delivery, but strategic direction still needs experienced operators.
              Master Builder keeps governance, quality, and executive decision-making attached to every workspace.
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

      <section id="briefing" className="py-24">
        <div className="mx-auto w-full max-w-[1440px] px-5 sm:px-8 lg:px-12">
          <div className="relative overflow-hidden rounded-[28px] bg-[#1a1c20] p-8 sm:p-12 lg:p-16">
            <div className="absolute right-0 top-0 h-full w-1/3 bg-[radial-gradient(circle_at_center,rgba(120,209,255,0.16),transparent_62%)] blur-[110px]" />
            <div className="relative max-w-3xl">
              <h2 className="text-4xl font-extrabold tracking-[-0.04em] text-white sm:text-5xl">
                Initiate your strategic briefing.
              </h2>
              <p className="mt-5 text-lg font-light leading-8 text-[#bdc8d0]">
                Start with the business objective. We will shape the workspace, the operating model, and the first
                governed path into delivery.
              </p>

              <form className="mt-10 space-y-6">
                <div className="grid gap-6 md:grid-cols-2">
                  <label className="block">
                    <span className="text-[11px] font-semibold uppercase tracking-[0.22em] text-[#bdc8d0]">Principal lead</span>
                    <input
                      type="text"
                      placeholder="e.g. Jonathan Ive"
                      className="mt-2 w-full rounded-md border-0 bg-[#0c0e12] px-5 py-4 text-white placeholder:text-[#3e484f] focus:ring-1 focus:ring-[#78d1ff]"
                    />
                  </label>
                  <label className="block">
                    <span className="text-[11px] font-semibold uppercase tracking-[0.22em] text-[#bdc8d0]">Enterprise category</span>
                    <input
                      type="text"
                      placeholder="e.g. Executive analytics"
                      className="mt-2 w-full rounded-md border-0 bg-[#0c0e12] px-5 py-4 text-white placeholder:text-[#3e484f] focus:ring-1 focus:ring-[#78d1ff]"
                    />
                  </label>
                </div>
                <label className="block">
                  <span className="text-[11px] font-semibold uppercase tracking-[0.22em] text-[#bdc8d0]">Strategic vision</span>
                  <textarea
                    rows={4}
                    placeholder="Describe the market opportunity and the organizational impact you want this workspace to support."
                    className="mt-2 w-full rounded-md border-0 bg-[#0c0e12] px-5 py-4 text-white placeholder:text-[#3e484f] focus:ring-1 focus:ring-[#78d1ff]"
                  />
                </label>
                <div className="flex flex-wrap gap-4">
                  <Link
                    href="/register"
                    className="rounded-md bg-[linear-gradient(135deg,#78d1ff_0%,#0b9bcf_100%)] px-6 py-3.5 text-sm font-extrabold tracking-tight text-[#002d40] transition hover:scale-[1.01]"
                  >
                    Initiate briefing
                  </Link>
                  <Link
                    href={primaryHref}
                    className="rounded-md bg-[#282a2e] px-6 py-3.5 text-sm font-semibold tracking-tight text-white transition hover:bg-[#333539]"
                  >
                    {session ? "Open workspace" : "Sign in"}
                  </Link>
                </div>
              </form>
            </div>
          </div>
        </div>
      </section>

      <footer className="bg-[#0c0e12] py-16">
        <div className="mx-auto grid w-full max-w-[1440px] gap-12 px-5 sm:px-8 md:grid-cols-3 lg:px-12">
          <div>
            <p className="text-base font-bold text-white">Master Builder</p>
            <p className="mt-4 max-w-xs text-sm leading-7 text-slate-500">
              Enterprise workspaces for governed delivery, operational clarity, and intelligent execution.
            </p>
          </div>
          <div className="space-y-4">
            <p className="text-[11px] font-bold uppercase tracking-[0.22em] text-white">Advisory services</p>
            <Link href="/register" className="block text-sm text-slate-500 transition-colors hover:text-white">
              Workspace launch
            </Link>
            <Link href="/privacy" className="block text-sm text-slate-500 transition-colors hover:text-white">
              Privacy policy
            </Link>
          </div>
          <div className="space-y-4">
            <p className="text-[11px] font-bold uppercase tracking-[0.22em] text-white">Entry points</p>
            <Link href={primaryHref} className="block text-sm text-slate-500 transition-colors hover:text-white">
              {session ? "Open workspace" : "Sign in"}
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
