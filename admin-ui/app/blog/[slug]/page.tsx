import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { blogPosts, getBlogPost } from "@/app/blog/posts";

type BlogPostPageProps = {
  params: Promise<{ slug: string }>;
};

export function generateStaticParams() {
  return blogPosts.map((post) => ({ slug: post.slug }));
}

export async function generateMetadata({ params }: BlogPostPageProps) {
  const { slug } = await params;
  const post = getBlogPost(slug);
  if (!post) {
    return {};
  }
  return {
    title: `${post.title} | Master Builder`,
    description: post.summary,
  };
}

export default async function BlogPostPage({ params }: BlogPostPageProps) {
  const { slug } = await params;
  const post = getBlogPost(slug);

  if (!post) {
    notFound();
  }

  return (
    <main className="min-h-screen bg-[#f4f6f2] text-[#17221f]">
      <header className="border-b border-[#d8ded8]">
        <div className="mx-auto flex w-full max-w-[900px] items-center justify-between px-5 py-5 sm:px-8">
          <Link href="/blog" className="inline-flex items-center gap-2 text-sm font-semibold text-[#52625b] transition hover:text-[#17221f]">
            <ArrowLeft className="h-4 w-4" />
            Blog
          </Link>
          <Link href="/" className="text-sm font-semibold text-[#52625b] transition hover:text-[#17221f]">
            Master Builder
          </Link>
        </div>
      </header>

      <article className="mx-auto w-full max-w-[900px] px-5 py-20 sm:px-8">
        <p className="text-xs font-semibold uppercase text-[#4d6b5f]">Governed AI delivery</p>
        <h1 className="mt-5 text-5xl font-semibold leading-none sm:text-6xl">{post.title}</h1>
        <p className="mt-7 text-xl leading-9 text-[#52625b]">{post.summary}</p>
        <div className="mt-14 space-y-7 border-t border-[#c7d0c8] pt-10 text-lg leading-9 text-[#35433e]">
          {post.body.map((paragraph) => (
            <p key={paragraph}>{paragraph}</p>
          ))}
        </div>
      </article>
    </main>
  );
}
