# Master Builder public website

This directory builds the public project overview as static HTML, CSS, JavaScript and locally served fonts. The authenticated dashboard lives in [`admin-ui`](../README.md#quick-start) and is hosted separately by operators. This site has no authentication packages, sessions, backend connection, account links or runtime environment secrets.

## Local development

Use Node.js 20.9 or newer and the public npm registry:

```bash
cd public-site
npm ci
npm run dev
```

Open `http://localhost:60003/`. The development server supports editing; deployment and browser checks use the static export instead.

```bash
npm run lint
npm run build
npm run test:static
npm run preview
```

Open `http://127.0.0.1:60003/`. `PUBLIC_SITE_PORT` selects a different local preview port. The preview serves only `out/` and returns HTTP 404 for unknown paths; it does not rewrite missing paths to the homepage.

## Browser checks

```bash
npx playwright install --with-deps chromium
npm run test:e2e
```

Build before running the tests. Playwright starts a new static preview on port 4603; `PUBLIC_SITE_TEST_PORT` selects an unused alternative. No running dashboard, API, database, authentication secret or private infrastructure is needed. Tests verify the real exported site, navigation, responsive layouts, keyboard access, privacy and font notices, unknown-path 404s and the absence of account/session entrypoints. Only external GitHub responses are mocked to exercise valid, malformed, private, unavailable and timeout conditions. Screenshots are saved in `test-results/`.

## Vercel deployment

The public address is `https://master-builder.vercel.app/`. Use these project settings:

| Setting | Value |
| --- | --- |
| Root directory | `public-site` |
| Framework | Other (`null`) |
| Node.js | 22.x |
| Install command | `npm ci` |
| Build command | `npm audit && npm run lint && npm run build` |
| Output directory | `out` |

`vercel.json` keeps the install/build/output and static routing configuration in source.
Only `out/` is served. There is no backend environment to configure. Production
domains are public; keep protection on preview deployments. The GitHub Public website
workflow additionally runs canonical font checks and browser tests from the complete
repository. Those checks read root `third_party` evidence outside the Vercel project.

This package uses `output: "export"` and `trailingSlash: true`, served at the domain root.
Do not add cookies, authentication, server actions, API routes, middleware or backend
imports; those belong in the self-hosted application. Unknown paths return 404.

The GitHub star widget makes one anonymous browser request to the public repository API with credentials omitted and no referrer. An invalid, private or unavailable response is explicitly displayed as “Stars unavailable”; no count is invented. With JavaScript disabled, the overview, feature links and setup instructions still work; the star count cannot load. The privacy page describes hosting and this request.

## Dependencies and licensing

Direct packages are pinned and the lockfile uses the public npm registry. Tailwind CSS 4.3.3 is used only here to avoid the vulnerable glob dependency chain in Tailwind 3. Builds fetch the selected Manrope font through Next.js; browsers receive its exported local files and do not contact Google Fonts.

First-party source is licensed under [`AGPL-3.0-only`](../LICENSE). Commercial use is permitted under its terms. Third-party components retain their licenses; see the root [third-party notices](../THIRD_PARTY_NOTICES.md). Manrope 4.504 is distributed under SIL OFL 1.1. `public/licenses/manrope-OFL.txt` and `public/licenses/manrope-NOTICE.txt` must remain byte-identical to the canonical files in [`third_party/licenses/manrope-4.504`](../third_party/licenses/manrope-4.504/).
