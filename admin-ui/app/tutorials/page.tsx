import Link from "next/link";

const tutorials = [
  {
    slug: "install-and-configure-master-builder-on-hetzner-linux",
    title: "Install And Configure Master Builder On Hetzner Linux",
    description: "External-operator guide for deploying Master Builder on Hetzner using a remote shell installer.",
  },
];

export default function TutorialsPage() {
  return (
    <main className="min-h-screen bg-[#111318] text-[#e2e2e8]">
      <header className="sticky top-0 z-50 border-b border-white/[0.06] bg-[#111318]/85 backdrop-blur-xl">
        <div className="mx-auto flex w-full max-w-[1440px] items-center justify-between px-5 py-5 sm:px-8 lg:px-12">
          <Link href="/" className="text-sm font-bold tracking-tight text-white sm:text-base">
            Master Builder
          </Link>
          <Link
            href="/login"
            className="rounded-md bg-[linear-gradient(135deg,#78d1ff_0%,#0b9bcf_100%)] px-4 py-2 text-xs font-bold tracking-tight text-[#002d40]"
          >
            Sign in
          </Link>
        </div>
      </header>

      <section className="relative isolate overflow-hidden">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_50%_-20%,rgba(120,209,255,0.12),transparent)]" />
        <div className="relative mx-auto w-full max-w-[1440px] px-5 py-16 sm:px-8 lg:px-12">
          <p className="text-[11px] font-bold uppercase tracking-[0.24em] text-[#78d1ff]">Documentation</p>
          <h1 className="mt-4 text-4xl font-extrabold tracking-[-0.04em] text-white sm:text-5xl">Tutorials</h1>
          <p className="mt-5 max-w-2xl text-base leading-8 text-[#bdc8d0]">
            Step-by-step deployment guides for external operators.
          </p>
        </div>
      </section>

      <section className="pb-20">
        <div className="mx-auto grid w-full max-w-[1440px] gap-6 px-5 sm:px-8 md:grid-cols-2 lg:px-12">
          {tutorials.map((tutorial) => (
            <article key={tutorial.slug} className="rounded-2xl border border-white/[0.08] bg-[#15171c] p-6">
              <h2 className="text-xl font-bold text-white">{tutorial.title}</h2>
              <p className="mt-3 text-sm leading-7 text-[#bdc8d0]">{tutorial.description}</p>
              <div className="mt-6 flex flex-wrap gap-3">
                <Link
                  href={`/tutorials/${tutorial.slug}`}
                  className="rounded-md bg-[linear-gradient(135deg,#78d1ff_0%,#0b9bcf_100%)] px-4 py-2 text-xs font-bold tracking-tight text-[#002d40]"
                >
                  Open tutorial
                </Link>
                <Link
                  href="/tutorials/hetzner-setup"
                  className="rounded-md bg-[#333539] px-4 py-2 text-xs font-semibold tracking-tight text-white transition hover:bg-[#3d4044]"
                >
                  Open setup form
                </Link>
              </div>
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}
