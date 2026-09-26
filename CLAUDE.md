# tensionix.github.io — brief

## What this site is
A self-maintaining GitHub Pages site that presents the public GitHub account Tensionix to ordinary people: a reorganisation of the account, not a code site. No commits, no file trees, no deep links, nothing hand-written. The author is Tensionix; the projects are Audion products. English only.

## Hard rules
- Zero hand-written content. Everything comes from the GitHub API at build time:
  - profile: name, avatar, bio, websiteUrl (tensionix.com);
  - repositories: public, owned, not forks, not archived, excluding `Tensionix` and `tensionix.github.io`: name, description, homepageUrl (audion.dev page), topics, latest release (tag, date, assets).
  A new repository or release must appear on the site by itself.
- Build and deploy with GitHub Actions (actions/deploy-pages): on push to main, on a daily schedule and by manual run. Fetch data with GITHUB_TOKEN (one GraphQL query is enough), write it to `_data/` inside the runner, build Jekyll, deploy. Never commit fetched data or images back to the repo.
- If fetching fails or returns no repositories, fail the job so the last good site stays online.
- GitHub disables scheduled workflows in public repos after 60 days without activity: add a step that re-enables the workflow via the API (permissions: actions: write). No dummy commits.
- Jekyll with your own layouts and CSS: no ready-made theme, no JS frameworks, JS only for the theme toggle. Exclude README.md and CLAUDE.md from the site.
- The Pages source is already set to "GitHub Actions". Do not touch repository settings.

## Pages: About (home) · Projects · Downloads
- About: avatar, name, bio, links to tensionix.com and the GitHub profile. Short.
- Projects: a card per repository with icon, display name, description, category chips from topics, version and release date, and the screenshot if present (click to enlarge). Links: homepageUrl (audion.dev) and the repository main page only.
- Downloads: per project, icon, display name, version, date and one button per `.zip` in the latest release: the plain zip is "Download", `_Full.zip` is "Download Full"; if the release has a single zip, one "Download". Show the size. Link directly to `browser_download_url`. Hide `.sha256`, `.json` and anything that is not `.zip`.
- Display name comes from the zip file name: `Audion_Office_OCR_AI_v1.8.2.zip` → "Audion Office OCR AI" (drop `_v<version>`, `_Full`, `.zip`; underscores become spaces). Fallback: the repository name.

## Images (taken from GitHub, copied into the build, not hotlinked)
- Icon, first found wins: `icons/<repo>.svg|png` in this repo → `Assets/app.svg` in the project repo → `system_core/icons/app.png` in the project repo → a neutral glyph. GraphQL `object(expression: "HEAD:<path>")` tells whether a file exists.
- Screenshot: `docs/screenshot.png` (the folder may be `Docs/`). Scale down to at most 1600 px wide at build time. No screenshot means no image; that is intended for five repositories.

## Look
- Font: JetBrains Mono (self-hosted woff2 or Google Fonts).
- Light and dark themes with a toggle; follow prefers-color-scheme and remember the choice in localStorage.
- Dark: purple neon `#8b6cff`, where the glow is a shadow of the line itself, plus faint neon spots in the background. Light: black `#171717`, no glow.
- It is an author's page presenting Audion products, not a download mirror. Every page has `<link rel="canonical">` pointing to https://tensionix.com.
- Works on a phone.

## Done means
Built and checked with real API data (unauthenticated REST is fine for a local check), including a project without a screenshot and one without an icon. Open a pull request with a short summary; the owner merges it, and the merge publishes the site.
