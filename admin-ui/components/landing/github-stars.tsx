"use client";

import { useEffect, useState } from "react";
import { Github, Star } from "lucide-react";

import { REPOSITORY_API_URL, REPOSITORY_NAME, REPOSITORY_URL } from "@/lib/public-repository";

type StarState = { status: "loading" } | { status: "ready"; count: number } | { status: "unavailable" };

export function GitHubStars() {
  const [state, setState] = useState<StarState>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    async function loadStars() {
      try {
        const response = await fetch(REPOSITORY_API_URL, {
          credentials: "omit",
          referrerPolicy: "no-referrer",
          headers: { Accept: "application/vnd.github+json" },
          // This bounds external IO; it is not synchronization or polling.
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(5_000)]),
        });
        if (!response.ok) throw new Error("GitHub repository statistics unavailable");
        const repository: unknown = await response.json();
        if (
          typeof repository !== "object" || repository === null ||
          !("full_name" in repository) || repository.full_name !== REPOSITORY_NAME ||
          !("private" in repository) || repository.private !== false ||
          !("stargazers_count" in repository) ||
          typeof repository.stargazers_count !== "number" ||
          !Number.isSafeInteger(repository.stargazers_count) || repository.stargazers_count < 0
        ) throw new Error("GitHub repository statistics invalid");
        if (active) setState({ status: "ready", count: repository.stargazers_count });
      } catch {
        // An unavailable count is explicit; no alternate or invented value is used.
        if (active) setState({ status: "unavailable" });
      }
    }

    void loadStars();
    return () => {
      active = false;
      controller.abort();
    };
  }, []);

  return (
    <a
      href={REPOSITORY_URL}
      aria-label="Star on GitHub"
      className="inline-flex items-center gap-2 rounded-lg border border-[#d7ddd7] bg-white px-3 py-2 text-xs font-semibold text-[#24352b] transition hover:border-[#698673] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#315c43] sm:text-sm"
    >
      <Github aria-hidden="true" className="h-4 w-4 shrink-0" />
      <span className="sr-only">Star on GitHub</span>
      <span aria-live="polite" aria-atomic="true">
        {state.status === "ready" ? `${state.count.toLocaleString("en-US")} stars` : state.status === "loading" ? "Loading stars" : "Stars unavailable"}
      </span>
      <Star aria-hidden="true" className="hidden h-3.5 w-3.5 sm:block" />
    </a>
  );
}
