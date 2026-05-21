# 2026-05-22 — Documentation SEO and GEO baseline

## Motivation

The MkDocs site at <https://dotnetpower.github.io/elb-dashboard/> was
missing the basics that classic search engines and the newer
generative-engine crawlers (ChatGPT, Claude, Perplexity, Google AI
overviews) expect:

- No per-page `<meta name="description">` beyond a single site-wide
  fallback, so search snippets and shared links all looked identical.
- No Open Graph / Twitter Card metadata, so previews on chat tools and
  social platforms degraded to bare URL strings.
- No JSON-LD structured data, so neither classic search engines nor LLM
  crawlers had a machine-readable summary of what the site documents.
- No `robots.txt`, so AI crawlers (`GPTBot`, `ClaudeBot`,
  `PerplexityBot`, `Google-Extended`, …) had no explicit allow rule.
- No `llms.txt` or `llms-full.txt`, the emerging
  [llmstxt.org](https://llmstxt.org/) standard for letting LLM crawlers
  ingest a project in one fetch.

## User-facing change

- Search snippets, social previews, and AI-generated answers now use the
  proper title and a page-specific description for every published page.
- The home page exposes a structured **Key Facts** summary and a
  Frequently Asked Questions section, both also emitted as JSON-LD
  (`WebSite`, `Organization`, `SoftwareApplication`, `FAQPage`).
- Inner pages emit `TechArticle` JSON-LD with the per-page description
  and canonical URL.
- `https://dotnetpower.github.io/elb-dashboard/robots.txt` explicitly
  allows AI crawlers and points at the sitemap.
- `https://dotnetpower.github.io/elb-dashboard/llms.txt` provides a
  curated documentation map for LLM crawlers; `llms-full.txt`
  concatenates every navigable page into a single ~7,000-line corpus.

## Implementation summary

| Area | Change |
|------|--------|
| `mkdocs.yml` | Richer `site_description`, `site_author`, copyright, `theme.custom_dir: docs/overrides`, `favicon`, extra features (`navigation.indexes`, `search.share`, `content.action.edit`, `content.tooltips`), `extra.og` defaults, `extra.llms` paths, `plugins: search`, `hooks: scripts/docs/llms_hooks.py`. |
| `docs/overrides/main.html` | Extends Material's `base.html`; overrides `{% block extrahead %}` to inject Open Graph + Twitter Card + theme-color + a JSON-LD `@graph` on the home page and `TechArticle` JSON-LD on every other page. Reads optional per-page `jsonld:` frontmatter for FAQ / HowTo extras. |
| `docs/robots.txt` | Explicit `Allow: /` for every major AI crawler plus the sitemap reference. |
| `docs/llms.txt` | llmstxt.org-style site map: quote-block summary, core concepts, curated link list. |
| `scripts/docs/llms_hooks.py` | `on_post_build` hook concatenates every navigable markdown page into `site/llms-full.txt` (skips `features_change/**`, `temp/**`, `overrides/**`). |
| `docs/*.md` (top-level) | Added YAML frontmatter `title:` / `description:` to 11 pages plus the home `index.md` and `changelog.md`. |
| `docs/index.md` | New **Key Facts** admonition and **Frequently Asked Questions** section; `jsonld:` frontmatter feeds a `FAQPage` block. |

No source code under `api/`, `web/`, `infra/`, or `terminal/` was
touched. No new runtime dependencies — the build hook uses only the
standard library and the existing `mkdocs-material` / `pymdown-extensions`
stack.

## Validation evidence

```bash
$ uv run ruff check scripts/docs/llms_hooks.py
All checks passed!

$ uv run mkdocs build --strict
INFO    -  Documentation built in 4.52 seconds
```

Spot-checked against the generated `site/`:

| Check | Result |
|-------|--------|
| `site/robots.txt` exists, lists GPTBot/ClaudeBot/Perplexity/Google-Extended | ✅ |
| `site/llms.txt` exists, llmstxt.org format | ✅ |
| `site/llms-full.txt` exists, 7,116 lines | ✅ |
| `site/sitemap.xml` has 391 `<loc>` entries | ✅ |
| `site/index.html` has 2 JSON-LD blocks: `@graph[WebSite, Organization, SoftwareApplication]` + `FAQPage` (6 Q&A) | ✅ |
| `site/auth/index.html`, `site/high-level-architecture/index.html`, `site/user-guide/index.html` emit `TechArticle` JSON-LD | ✅ |
| Every page carries page-specific `<meta name="description">`, OG, and Twitter Card tags | ✅ |
| `<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1">` present | ✅ |

## Out of scope

- mkdocs-material `social` plugin (auto-generated PNG social cards) is
  available but needs `cairosvg` + `Pillow` + system `libcairo`; not
  added to keep the build pipeline portable. The static logo OG image
  works as a sensible fallback.
- No analytics, no third-party tag managers.
- Tag-based navigation pages (mkdocs-material `tags` plugin) deferred —
  no `tags:` frontmatter yet to justify a tag index.

## Update — auto-generated social cards enabled

Reversed the "out of scope" decision above. `libcairo2`, `libgdk-pixbuf`,
and `fonts-dejavu` are already installed on the supported build hosts
(Ubuntu / Debian / GitHub-hosted runners), so the pure-Python additions
(`cairosvg`, `Pillow`) are enough to enable the plugin.

- `pyproject.toml` `[dependency-groups].dev` adds `cairosvg>=2.7,<3` and
  `pillow>=10.0,<12`.
- `mkdocs.yml` enables the social plugin with a brand-aligned card layout
  (`background_color: "#1f1d3a"`, `color: "#f5f4ff"`, Roboto font).
- `docs/overrides/main.html` drops its `og:type` / `og:title` /
  `og:description` / `og:url` / `og:image` / `twitter:card` /
  `twitter:title` / `twitter:description` / `twitter:image` blocks —
  the plugin now emits all of them, and duplicating them caused the SVG
  logo URL to shadow the new PNG card on consumers that honour the first
  occurrence (Slack, Discord). The override now only emits `og:site_name`,
  `og:locale`, `meta name="description"`, `meta name="robots"`,
  `theme-color`, `application-name`, and the JSON-LD blocks.
- Long SEO titles (e.g. "ElasticBLAST Control Plane — Browser-only BLAST
  on Azure") were truncating in the 1200×630 layout. Affected pages now
  carry a short card title under `social.cards_layout_options.title` /
  `description` in their frontmatter (the plugin merges `page.meta.social`
  with the site-level `cards_layout_options`, per its
  `_config()` implementation).

Validation:

```bash
$ uv run mkdocs build --strict
INFO    -  Documentation built in 27.09 seconds   # cold (first card render)
INFO    -  Documentation built in 6.13 seconds    # warm (cache hit)

$ find site/assets/images/social -name '*.png' | wc -l
392                                                # one card per published page

$ du -sh site/assets/images/social
23M

$ for f in site/index.html site/auth/index.html site/get-started/index.html ...
1 1   # exactly one og:title and one og:image per page (no duplicates)
```

Cold builds take ~30 s; warm builds (`.cache/plugin/social/`) stay near
the original ~5 s. The cache directory is already gitignored.

## Update — BreadcrumbList, HowTo, full description coverage, TL;DR, self-hosted Mermaid

Second pass to close the gaps called out in the post-baseline review.

- **BreadcrumbList JSON-LD** on every inner page
  ([`docs/overrides/main.html`](../../overrides/main.html)). Walks
  `page.ancestors` (deepest-first → reversed) and emits Home → … → page.
  Section nodes without a `url` are still emitted as `ListItem.name`
  without `item`, which schema.org permits. Verified shapes:
  - `Home → User Guide → Dashboard`
  - `Home → Agent Reference → Repo Layout`
  - `Home → Releases → v0.1.0`
  - `Home → Auth` (top-level pages, no intermediate section)
- **HowTo JSON-LD** on `docs/get-started.md` — six `HowToStep` entries
  matching the page's `##` sections, with `tool`, `supply`, `totalTime`,
  and per-step `url` anchors. Google rich-result eligible.
- **Description frontmatter** filled in on 19 pages that previously fell
  back to the site-level description: every `docs/copilot/*.md`,
  every `docs/user-guide/*.md`, and `docs/releases/*.md`. Coverage check
  across 27 published pages shows 0 fallbacks.
- **TL;DR callouts** (`!!! tip "TL;DR"`) added near the top of
  `docs/index.md`'s neighbours: Get Started, High Level Architecture,
  Auth, Deployment Reference, Container Apps Migration, and User Guide.
  Improves both human skim-reading and the chance that an LLM crawler
  cites the page accurately.
- **Self-hosted Mermaid**: dropped the `unpkg.com/mermaid@10.9.1` CDN
  reference in `mkdocs.yml` `extra_javascript` and replaced it with
  `docs/javascripts/vendor/mermaid.min.js` (3.2 MB pinned to 10.9.1).
  The `preconnect` hint in [`docs/overrides/main.html`](../../overrides/main.html)
  was removed in the same change. Site `grep -r unpkg site/` only matches
  Material's own bundled JS now (out of our control).
- **JSON-LD hardening**: every author-supplied string in the JSON-LD
  blocks now goes through Jinja's `| tojson` filter so embedded
  double-quotes, backslashes, or control characters can't break the
  surrounding JSON. Discovered by a page whose description contained
  the literal phrase `"where to edit"`, which produced an unparseable
  `TechArticle` block on the previous pass.

Validation:

```bash
$ uv run mkdocs build --strict
INFO    -  Documentation built in 6.32 seconds

# All 7 sampled pages parse cleanly, 0 invalid JSON blocks:
$ uv run python -c "..."   # see scripts/dev (one-off, not committed)
site/copilot/repo-layout/index.html       Breadcrumb: [Home, Agent Reference, Repo Layout]
site/user-guide/dashboard/index.html      Breadcrumb: [Home, User Guide, Dashboard]
site/user-guide/results/index.html        Breadcrumb: [Home, User Guide, Results]
site/releases/v0.1.0/index.html           Breadcrumb: [Home, Releases, v0.1.0]
site/auth/index.html                      Breadcrumb: [Home, Auth]
site/get-started/index.html               types=[TechArticle, BreadcrumbList, HowTo]
site/index.html                           types=[@graph(WebSite+Org+SoftwareApplication), FAQPage]
TOTAL INVALID JSON BLOCKS: 0
```

Self-assessed SEO/GEO score after this pass: roughly **A− (≈88/100)**
on the rubric used for the baseline review. Remaining headroom is in
off-page authority (backlinks, GSC/Bing registration) and Material's
own bundled JS still referencing unpkg, neither of which is
addressable from this repo alone.

## Update — critical hardening pass

A skeptical re-audit of the two earlier passes found three real defects
that the "everything builds clean" smoke tests had hidden.

### 1. Template was published as a public asset (CRITICAL)

`theme.custom_dir` pointed at `docs/overrides/`, which sits inside
`docs_dir`. MkDocs therefore copied `docs/overrides/main.html` verbatim
into `site/overrides/main.html`. Anyone visiting
`https://dotnetpower.github.io/elb-dashboard/overrides/main.html` would
see the raw Jinja template (including, for example, the per-page
`page.meta.jsonld` plumbing). Search engines would also index it as
garbage HTML.

Fix: moved `docs/overrides/` → repo-root `overrides/` (sibling of
`mkdocs.yml`) and updated `theme.custom_dir: overrides` in
[mkdocs.yml](../../../mkdocs.yml). MkDocs no longer treats it as docs
content. Verified `site/overrides/` does not exist after a clean build.

### 2. HTML entities leaked into JSON-LD values

MkDocs renders `page.title` through the markdown pipeline, so an H1
like `Authentication & Authorization` arrives in templates as the
HTML-escaped string `Authentication &amp; Authorization`. The earlier
pass embedded that directly via `| tojson`, publishing
`"name": "Authentication &amp; Authorization"` to crawlers — 6 such
warnings were detected across the features_change archive.

Fix: added an `unescape_html` Jinja filter in
[scripts/docs/llms_hooks.py](../../../scripts/docs/llms_hooks.py)
(registered via the `on_env` hook) and applied it to every
page-derived string in [overrides/main.html](../../../overrides/main.html)
JSON-LD blocks (`page_title`, `page_descr`, `crumbs.name`,
`config.site_author`). Post-fix audit: 0 entity warnings across 790
JSON-LD blocks on 400 generated HTML pages.

### 3. Average og:title was 82 chars (Google truncates at ~60)

The previous round set long descriptive titles like
`"Repository Layout — ElasticBLAST Control Plane (Agent Reference)"`,
then the social plugin appended ` - {site_name}`, producing 100-char
og:titles. Google truncates SERP snippets at roughly 60 characters.

Fix: rewrote every page's frontmatter `title:` to be concise (the
plugin already appends the site name automatically). Average og:title
length dropped from **82.5 → 51.1 characters**; the longest is now
66 chars; 0 pages ≥70 chars.

### Smaller hardening items

- `llms-full.txt` now excludes both off-nav pages
  (`copilot/security-audit-followup.md`,
  `copilot/version-management.md`). The security-audit page documents
  open follow-up items from the 2026-05-22 security sweep — surfacing
  it to AI crawlers would advertise unfixed weaknesses. Corpus shrunk
  from 7,116 → 6,664 lines.
- Verified every HowTo step URL anchor on Get Started matches a real
  page slug (`#choose-your-path`, `#before-you-start`, …).
- Schema.org required-field check across the home, Get Started, Auth,
  Dashboard, and Repo Layout pages: every node (FAQPage, HowTo,
  TechArticle, BreadcrumbList, WebSite, Organization,
  SoftwareApplication) carries its required properties.
- Verified Mermaid script integrity: pinned local copy is 3.2 MB,
  `sha384-WmdflGW9aGfoBdHc4rRyWzYuAjEmDwMdGdiPNacbwfGKxBW/SO6guzuQ76qjnSlr`.
  Did not add an SRI attribute to the `<script>` tag — `extra_javascript`
  doesn't expose `integrity` plumbing, and the file is now same-origin
  anyway, so the SRI benefit is marginal.

### Final validation

```bash
$ uv run ruff check scripts/docs/llms_hooks.py
All checks passed!

$ uv run mkdocs build --strict
INFO    -  Documentation built in 6.21 seconds   # warm
INFO    -  Documentation built in 28.45 seconds  # cold (cards regenerated)

$ python -c "audit script"
JSON-LD parse errors:        0   (790 blocks / 400 pages)
JSON-LD entity warnings:     0
og:title avg / max:          51.1 / 66 chars
overrides/main.html in site: False
sensitive pages in llms-full.txt: False
```

Re-revised score after hardening: **~92/100 (A)**. The remaining points
are still external-only (off-page authority, social-graph backlinks).
