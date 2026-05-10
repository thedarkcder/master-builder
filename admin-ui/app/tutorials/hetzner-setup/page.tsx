import Link from "next/link";

import { HetznerSetupForm } from "./setup-form";

export default function HetznerSetupPage() {
  return (
    <main className="min-h-screen bg-[#111318] text-[#e2e2e8]">
      <header className="sticky top-0 z-50 border-b border-white/[0.06] bg-[#111318]/85 backdrop-blur-xl">
        <div className="mx-auto flex w-full max-w-[1440px] items-center justify-between px-5 py-5 sm:px-8 lg:px-12">
          <Link href="/tutorials" className="text-sm font-bold tracking-tight text-white sm:text-base">
            Tutorials
          </Link>
          <Link
            href="/tutorials/install-and-configure-master-builder-on-hetzner-linux"
            className="rounded-md bg-[#333539] px-4 py-2 text-xs font-semibold tracking-tight text-white"
          >
            Back to tutorial
          </Link>
        </div>
      </header>

      <section className="relative isolate overflow-hidden">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_50%_-20%,rgba(120,209,255,0.12),transparent)]" />
        <div className="relative mx-auto w-full max-w-[1200px] px-5 py-14 sm:px-8 lg:px-12">
          <p className="text-[11px] font-bold uppercase tracking-[0.24em] text-[#78d1ff]">Hetzner Setup</p>
          <h1 className="mt-4 text-4xl font-extrabold leading-[0.95] tracking-[-0.04em] text-white sm:text-5xl">
            Generate Deploy Inputs
          </h1>
          <p className="mt-6 max-w-3xl text-base leading-8 text-[#bdc8d0]">
            Fill once to generate both launch artifacts: shell launch command and cloud-init user-data.
          </p>
        </div>
      </section>

      <section className="pb-20">
        <div className="mx-auto w-full max-w-[1200px] px-5 sm:px-8 lg:px-12">
          <HetznerSetupForm />
        </div>
      </section>
    </main>
  );
}
