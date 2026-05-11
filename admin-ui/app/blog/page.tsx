import Link from "next/link";
import { ArrowLeft, ArrowRight } from "lucide-react";

import { blogPosts } from "@/app/blog/posts";

export default function BlogPage() {
  return (
    <main className="min-h-screen bg-[#f4f6f2] text-[#17221f]">
      <header className="border-b border-[#d8ded8]">
        <div className="mx-auto flex w-full max-w-[1200px] items-center justify-between px-5 py-5 sm:px-8">
          <Link href="/" className="inline-flex items-center gap-2 text-sm font-semibold text-[#52625b] transition hover:text-[#17221f]">
            <ArrowLeft className="h-4 w-4" />
            Master Builder
          </Link>
          <Link href="/login" className="text-sm font-semibold text-[#52625b] transition hover:text-[#17221f]">
            Sign in
          </Link>
        </div>
      </header>

      <section className="border-b border-[#d8ded8] py-20">
        <div className="mx-auto w-full max-w-[1200px] px-5 sm:px-8">
          <p className="text-xs font-semibold uppercase text-[#4d6b5f]">Governed AI delivery</p>
          <h1 className="mt-5 max-w-4xl text-5xl font-semibold leading-none sm:text-6xl">
            Thinking for teams scaling AI-assisted engineering responsibly.
          </h1>
          <p className="mt-7 max-w-3xl text-lg leading-8 text-[#52625b]">
            Notes on software delivery, review bottlenecks, product ambiguity, and the operating model shift from
            traditional engineering teams to governed AI delivery organisations.
          </p>
        </div>
      </section>

      <section className="py-16">
        <div className="mx-auto w-full max-w-[1200px] px-5 sm:px-8">
          <div className="grid gap-px bg-[#d8ded8] md:grid-cols-2">
            {blogPosts.map((post) => (
              <Link key={post.title} href={`/blog/${post.slug}`} className="group bg-[#fbfcfa] p-6 transition-colors hover:bg-[#eef1ec]">
                <h2 className="text-2xl font-semibold leading-8">{post.title}</h2>
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
    </main>
  );
}
