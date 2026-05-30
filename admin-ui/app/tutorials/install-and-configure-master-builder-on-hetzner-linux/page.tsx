import Link from "next/link";

export default function HetznerInstallTutorialPage() {
  return (
    <main className="min-h-screen bg-[#111318] text-[#e2e2e8]">
      <header className="sticky top-0 z-50 border-b border-white/[0.06] bg-[#111318]/85 backdrop-blur-xl">
        <div className="mx-auto flex w-full max-w-[1440px] items-center justify-between px-5 py-5 sm:px-8 lg:px-12">
          <Link href="/tutorials" className="text-sm font-bold tracking-tight text-white sm:text-base">
            Tutorials
          </Link>
          <Link
            href="/tutorials/hetzner-setup"
            className="rounded-md bg-[linear-gradient(135deg,#78d1ff_0%,#0b9bcf_100%)] px-4 py-2 text-xs font-bold tracking-tight text-[#002d40]"
          >
            Open setup form
          </Link>
        </div>
      </header>

      <section className="relative isolate overflow-hidden">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_50%_-20%,rgba(120,209,255,0.12),transparent)]" />
        <div className="relative mx-auto w-full max-w-[1200px] px-5 py-16 sm:px-8 lg:px-12">
          <p className="text-[11px] font-bold uppercase tracking-[0.24em] text-[#78d1ff]">Hetzner Tutorial</p>
          <h1 className="mt-4 text-4xl font-extrabold leading-[0.95] tracking-[-0.04em] text-white sm:text-5xl">
            Install And Configure Master Builder On Linux
          </h1>
          <p className="mt-6 max-w-3xl text-base leading-8 text-[#bdc8d0]">
            This guide is for external operators deploying Master Builder themselves on a single Hetzner server.
            The install entrypoint is a remote shell command.
          </p>
        </div>
      </section>

      <section className="pb-20">
        <div className="mx-auto w-full max-w-[1200px] space-y-4 px-5 sm:px-8 lg:px-12">
          <article className="rounded-2xl border border-white/[0.08] bg-[#15171c] p-6">
            <h2 className="text-xl font-bold text-white">Prerequisites</h2>
            <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-7 text-[#bdc8d0]">
              <li>Hetzner Cloud account, project, and API token</li>
              <li>An SSH key already registered in Hetzner Cloud</li>
              <li>Linux terminal with network access</li>
              <li>Master Builder secrets for admin, state, encryption, and auth</li>
            </ul>
          </article>

          <article className="rounded-2xl border border-white/[0.08] bg-[#15171c] p-6">
            <h2 className="text-xl font-bold text-white">Step 1: Run installer</h2>
            <p className="mt-3 text-sm leading-7 text-[#bdc8d0]">
              Publish your installer endpoint, then operators run:
            </p>
            <pre className="mt-3 overflow-x-auto rounded-xl border border-[#78d1ff]/20 bg-[#0f1218] p-4 text-sm text-[#d9e4ec]">
              <code>curl -fsSL https://install.masterbuilder.ai/hetzner/install.sh | bash</code>
            </pre>
          </article>

          <article className="rounded-2xl border border-white/[0.08] bg-[#15171c] p-6">
            <h2 className="text-xl font-bold text-white">Step 2: Enter prompted values</h2>
            <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-7 text-[#bdc8d0]">
              <li>
                <code>HCLOUD_TOKEN</code>, <code>HCLOUD_SSH_KEY_NAME</code>, server name/type/location/image
              </li>
              <li>
                <code>ORCHESTRATOR_ADMIN_PASSWORD</code>
              </li>
              <li>
                <code>ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET</code>
              </li>
              <li>
                <code>ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET</code>
              </li>
              <li>
                <code>ORCHESTRATOR_SECRETS_ENCRYPTION_KEY</code>
              </li>
              <li>
                <code>NEXTAUTH_SECRET</code>
              </li>
              <li>
                <code>ORCHESTRATOR_EMAIL_FROM_ADDRESS</code> and provider secrets
              </li>
              <li>
                <code>LLAMA_CPP_MODEL_URL</code>, <code>LLAMA_CPP_MODEL_PATH</code>, <code>ENABLE_LLAMA_GPU</code>,{" "}
                <code>LLAMA_CPP_N_GPU_LAYERS</code>
              </li>
            </ul>
          </article>

          <article className="rounded-2xl border border-white/[0.08] bg-[#15171c] p-6">
            <h2 className="text-xl font-bold text-white">Step 3: Validate deployment</h2>
            <pre className="mt-3 overflow-x-auto rounded-xl border border-[#78d1ff]/20 bg-[#0f1218] p-4 text-sm text-[#d9e4ec]">
              <code>{`curl -fsSL http://SERVER_IP:60001/health
# open in browser
http://SERVER_IP:60002`}</code>
            </pre>
            <p className="mt-3 text-sm leading-7 text-[#bdc8d0]">
              If health is not <code>ok</code>, inspect cloud-init output and docker container logs on the host.
            </p>
          </article>
        </div>
      </section>
    </main>
  );
}
