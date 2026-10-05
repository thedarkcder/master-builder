import Link from "next/link";

export default function PrivacyPage() {
  return (
    <main className="mx-auto min-h-screen max-w-3xl px-5 py-12 text-[#15243b] sm:px-8">
      <h1 className="text-3xl font-semibold tracking-tight">Public website privacy</h1>
      <p className="mt-4 text-sm text-slate-600">Last updated: October 5, 2026</p>
      <section className="mt-8 space-y-3 text-sm leading-7 text-slate-600">
        <h2 className="text-lg font-semibold text-[#15243b]">Static project website</h2>
        <p>Master Builder’s public website provides project information and source links. It has no accounts, login sessions, application cookies or analytics trackers. The authenticated administration dashboard is a separate application that operators host in their own environment.</p>
        <p>Vercel hosts this website and receives normal network information when you visit, such as your IP address. Its handling of that information is governed by Vercel’s privacy policy. Font files are served with the website; your browser does not contact Google Fonts.</p>
      </section>
      <section className="mt-8 space-y-3 text-sm leading-7 text-slate-600">
        <h2 className="text-lg font-semibold text-[#15243b]">Public homepage statistics</h2>
        <p>Your browser contacts api.github.com to read the public repository’s star count. No credentials or cookies are sent with this request. GitHub receives normal network metadata, such as your IP address. If the request is unavailable, the website says “Stars unavailable”.</p>
        <p><a href="https://vercel.com/legal/privacy-policy" className="font-medium underline underline-offset-4">Vercel privacy policy</a></p>
        <p><a href="https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement" className="font-medium underline underline-offset-4">GitHub privacy statement</a></p>
      </section>
      <p className="mt-10"><Link href="/" className="font-medium text-[#245ac7] underline underline-offset-4">Back to Master Builder</Link></p>
    </main>
  );
}
