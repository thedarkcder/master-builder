"use client";

import { useMemo, useState } from "react";

type FormState = {
  hcloudToken: string;
  sshKey: string;
  serverName: string;
  serverType: string;
  location: string;
  image: string;
  repoUrl: string;
  repoRef: string;
  llamaModelUrl: string;
  llamaModelPath: string;
  llamaPort: string;
  enableLlamaGpu: string;
  llamaGpuLayers: string;
  llamaCtxSize: string;
  adminPassword: string;
  githubState: string;
  atlassianState: string;
  encKey: string;
  nextAuthSecret: string;
  emailFrom: string;
  emailProvider: string;
  resendKey: string;
};

const defaults: FormState = {
  hcloudToken: "",
  sshKey: "default",
  serverName: "master-builder-01",
  serverType: "cpx31",
  location: "nbg1",
  image: "ubuntu-24.04",
  repoUrl: "https://github.com/your-org/master-builder.git",
  repoRef: "main",
  llamaModelUrl: "",
  llamaModelPath: "/models/gemma-4.gguf",
  llamaPort: "8080",
  enableLlamaGpu: "true",
  llamaGpuLayers: "999",
  llamaCtxSize: "8192",
  adminPassword: "",
  githubState: "",
  atlassianState: "",
  encKey: "",
  nextAuthSecret: "",
  emailFrom: "no-reply@example.com",
  emailProvider: "resend",
  resendKey: "",
};

const requiredKeys: (keyof FormState)[] = [
  "hcloudToken",
  "sshKey",
  "adminPassword",
  "githubState",
  "atlassianState",
  "encKey",
  "nextAuthSecret",
  "emailFrom",
];

function quote(value: string): string {
  return `'${value.replace(/'/g, `'\"'\"'`)}'`;
}

export function HetznerSetupForm() {
  const [state, setState] = useState<FormState>(defaults);
  const [status, setStatus] = useState<string>("");

  const missing = useMemo(
    () => requiredKeys.filter((k) => state[k].trim() === ""),
    [state]
  );

  const launchCommand = useMemo(() => {
    const lines = [
      `export HCLOUD_TOKEN=${quote(state.hcloudToken)}`,
      `export HCLOUD_SSH_KEY_NAME=${quote(state.sshKey)}`,
      `export SERVER_NAME=${quote(state.serverName)}`,
      `export SERVER_TYPE=${quote(state.serverType)}`,
      `export SERVER_LOCATION=${quote(state.location)}`,
      `export SERVER_IMAGE=${quote(state.image)}`,
      `export REPO_URL=${quote(state.repoUrl)}`,
      `export REPO_REF=${quote(state.repoRef)}`,
      `export LLAMA_CPP_MODEL_URL=${quote(state.llamaModelUrl)}`,
      `export LLAMA_CPP_MODEL_PATH=${quote(state.llamaModelPath)}`,
      `export LLAMA_CPP_PORT=${quote(state.llamaPort)}`,
      `export ENABLE_LLAMA_GPU=${quote(state.enableLlamaGpu)}`,
      `export LLAMA_CPP_N_GPU_LAYERS=${quote(state.llamaGpuLayers)}`,
      `export LLAMA_CPP_CTX_SIZE=${quote(state.llamaCtxSize)}`,
      `export ORCHESTRATOR_ADMIN_PASSWORD=${quote(state.adminPassword)}`,
      `export ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET=${quote(state.githubState)}`,
      `export ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET=${quote(state.atlassianState)}`,
      `export ORCHESTRATOR_SECRETS_ENCRYPTION_KEY=${quote(state.encKey)}`,
      `export NEXTAUTH_SECRET=${quote(state.nextAuthSecret)}`,
      `export ORCHESTRATOR_EMAIL_FROM_ADDRESS=${quote(state.emailFrom)}`,
      `export ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER=${quote(state.emailProvider)}`,
      `export ORCHESTRATOR_RESEND_API_KEY=${quote(state.resendKey)}`,
      "curl -fsSL https://install.masterbuilder.ai/hetzner/install.sh | bash",
    ];
    return lines.join("\n");
  }, [state]);

  const cloudInit = useMemo(() => {
    const lines = [
      "#cloud-config",
      "package_update: true",
      "runcmd:",
      `  - export REPO_URL=${quote(state.repoUrl)}`,
      `  - export REPO_REF=${quote(state.repoRef)}`,
      `  - export LLAMA_CPP_MODEL_URL=${quote(state.llamaModelUrl)}`,
      `  - export LLAMA_CPP_MODEL_PATH=${quote(state.llamaModelPath)}`,
      `  - export LLAMA_CPP_PORT=${quote(state.llamaPort)}`,
      `  - export ENABLE_LLAMA_GPU=${quote(state.enableLlamaGpu)}`,
      `  - export LLAMA_CPP_N_GPU_LAYERS=${quote(state.llamaGpuLayers)}`,
      `  - export LLAMA_CPP_CTX_SIZE=${quote(state.llamaCtxSize)}`,
      `  - export ORCHESTRATOR_ADMIN_PASSWORD=${quote(state.adminPassword)}`,
      `  - export ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET=${quote(state.githubState)}`,
      `  - export ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET=${quote(state.atlassianState)}`,
      `  - export ORCHESTRATOR_SECRETS_ENCRYPTION_KEY=${quote(state.encKey)}`,
      `  - export NEXTAUTH_SECRET=${quote(state.nextAuthSecret)}`,
      `  - export ORCHESTRATOR_EMAIL_FROM_ADDRESS=${quote(state.emailFrom)}`,
      `  - export ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER=${quote(state.emailProvider)}`,
      `  - export ORCHESTRATOR_RESEND_API_KEY=${quote(state.resendKey)}`,
      `  - git clone ${quote(state.repoUrl)} /opt/master-builder-bootstrap`,
      "  - bash /opt/master-builder-bootstrap/deploy/hetzner/bootstrap.sh",
    ];
    return lines.join("\n");
  }, [state]);

  const onCopy = async (text: string, label: string) => {
    try {
      if (missing.length > 0) {
        setStatus(`Missing required fields: ${missing.join(", ")}`);
        return;
      }
      await navigator.clipboard.writeText(text);
      setStatus(`${label} copied.`);
    } catch {
      setStatus(`Failed to copy ${label.toLowerCase()}.`);
    }
  };

  const input = (key: keyof FormState, label: string, type = "text") => (
    <label className="block">
      <span className="mb-1 block text-xs font-semibold uppercase tracking-[0.14em] text-[#8fa0ad]">{label}</span>
      <input
        type={type}
        value={state[key]}
        onChange={(e) => setState((prev) => ({ ...prev, [key]: e.target.value }))}
        className="w-full rounded-md border border-white/[0.12] bg-[#0f1218] px-3 py-2 text-sm text-white outline-none focus:border-[#78d1ff]"
      />
    </label>
  );

  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-white/[0.08] bg-[#15171c] p-6">
        <h2 className="text-xl font-bold text-white">Hetzner setup inputs</h2>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          {input("hcloudToken", "HCLOUD_TOKEN", "password")}
          {input("sshKey", "HCLOUD_SSH_KEY_NAME")}
          {input("serverName", "SERVER_NAME")}
          {input("serverType", "SERVER_TYPE")}
          {input("location", "SERVER_LOCATION")}
          {input("image", "SERVER_IMAGE")}
          {input("repoUrl", "REPO_URL")}
          {input("repoRef", "REPO_REF")}
          {input("llamaModelUrl", "LLAMA_CPP_MODEL_URL (GGUF URL)")}
          {input("llamaModelPath", "LLAMA_CPP_MODEL_PATH")}
          {input("llamaPort", "LLAMA_CPP_PORT")}
          {input("enableLlamaGpu", "ENABLE_LLAMA_GPU (true/false)")}
          {input("llamaGpuLayers", "LLAMA_CPP_N_GPU_LAYERS")}
          {input("llamaCtxSize", "LLAMA_CPP_CTX_SIZE")}
          {input("adminPassword", "ORCHESTRATOR_ADMIN_PASSWORD", "password")}
          {input("githubState", "ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET", "password")}
          {input("atlassianState", "ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET", "password")}
          {input("encKey", "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", "password")}
          {input("nextAuthSecret", "NEXTAUTH_SECRET", "password")}
          {input("emailFrom", "ORCHESTRATOR_EMAIL_FROM_ADDRESS")}
          {input("emailProvider", "ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER")}
          {input("resendKey", "ORCHESTRATOR_RESEND_API_KEY", "password")}
        </div>
        <p className="mt-4 text-sm text-[#9ab0bf]">
          {missing.length > 0 ? `Missing required fields: ${missing.join(", ")}` : "All required fields are present."}
        </p>
      </section>

      <section className="rounded-2xl border border-white/[0.08] bg-[#15171c] p-6">
        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            onClick={() => void onCopy(launchCommand, "Launch command")}
            className="rounded-md bg-[linear-gradient(135deg,#78d1ff_0%,#0b9bcf_100%)] px-4 py-2 text-xs font-bold tracking-tight text-[#002d40]"
          >
            Copy launch command
          </button>
          <button
            type="button"
            onClick={() => void onCopy(cloudInit, "Cloud-init")}
            className="rounded-md bg-[#333539] px-4 py-2 text-xs font-semibold tracking-tight text-white"
          >
            Copy cloud-init
          </button>
        </div>
        {status ? <p className="mt-3 text-sm text-[#9ab0bf]">{status}</p> : null}
      </section>

      <section className="rounded-2xl border border-white/[0.08] bg-[#15171c] p-6">
        <h2 className="text-xl font-bold text-white">Generated launch command</h2>
        <textarea
          readOnly
          value={launchCommand}
          className="mt-3 h-64 w-full rounded-lg border border-[#78d1ff]/20 bg-[#0f1218] p-3 font-mono text-xs text-[#d9e4ec]"
        />
      </section>

      <section className="rounded-2xl border border-white/[0.08] bg-[#15171c] p-6">
        <h2 className="text-xl font-bold text-white">Generated cloud-init</h2>
        <textarea
          readOnly
          value={cloudInit}
          className="mt-3 h-64 w-full rounded-lg border border-[#78d1ff]/20 bg-[#0f1218] p-3 font-mono text-xs text-[#d9e4ec]"
        />
      </section>
    </div>
  );
}
