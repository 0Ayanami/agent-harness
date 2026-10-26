# Frontend migration

The executable workbench is a small migration of the existing `web/` teaching
site. The original site, its chapter pages, generated course data, simulations,
and extraction pipeline were not copied into this standalone repository. This directory uses the same Next.js,
React, Tailwind, icons, and motion packages already selected by that site.

## Source map

Source paths below refer to the original teaching repository; destination paths
are relative to this repository's `interfaces/web/frontend/`. Files marked "unchanged" are
copied exactly; adapted components retain the existing rendering implementation.

| Source in original `web/` | Destination in `interfaces/web/frontend/` | Changes |
| --- | --- | --- |
| `src/app/globals.css` | `src/app/globals.css` | Unchanged theme, colors, responsive rules, documentation and code styles. |
| `src/components/ui/card.tsx` | same relative path | Unchanged. |
| `src/components/ui/badge.tsx` | same relative path | Unchanged layer badges and colors. |
| `src/components/ui/tabs.tsx` | same relative path | Unchanged. Used for conversation/trace and project state. |
| `src/components/layout/header.tsx` | same relative path | Retains brand, language switching, responsive navigation, and theme toggle. Adds the workbench item; course links open the original teaching site. Adds accessible button labels. |
| `src/components/layout/sidebar.tsx` | same relative path | Retains chapter grouping and styles in an expandable course section. Adds real session creation and selection. |
| `src/components/simulator/simulator-message.tsx` | same relative path | Retains original icons, message type colors, borders, and motion. Localizes labels, preserves whitespace, and receives only actual runtime messages and tool events. |
| `src/lib/utils.ts` | same relative path | Unchanged. |
| `src/lib/constants.ts` | same relative path | Unchanged chapter navigation metadata; no generated course source/document data. |
| `src/lib/i18n.tsx` | same relative path | Retains provider/hooks, adds typed access and a workbench namespace. |
| `src/i18n/messages/{en,zh,ja}.json` | same relative paths | Unchanged teaching dictionaries. |
| `src/types/agent-data.ts` | same relative path | Unchanged component types. |
| `src/app/[locale]/layout.tsx` | same relative path | Retains locale provider, header, main shell, and local theme initialization. Metadata describes the runtime workbench. |
| `src/app/page.tsx` | `src/app/(entry)/page.tsx` | Retains the default Chinese redirect; the entry route has its own root layout for webpack builds. |
| `src/app/favicon.ico` | same relative path | Unchanged original site favicon. |
| `tsconfig.json` | same relative path | Preserves compiler options; stores incremental state in the repository's `.artifacts/cache/typescript/` directory. |
| `postcss.config.mjs` | same relative path | Unchanged. |
| `next.config.ts` | same relative path | Retains production static export. Development adds the Python API proxy. |
| `package.json` | `package.json` | Keeps required original dependencies, excludes course extraction and unused document/diagram packages. |

## Necessary runtime integration

`components/runtime/` contains the page composition, approval card, and project
state view, using the migrated UI components. `hooks/use-agent-session.ts`
implements actual session loading, stream subscriptions, cancellation, approval
responses, reconnects, and refresh recovery. `lib/agent-api.ts` is the typed HTTP
adapter. No teaching scenario or simulated message is passed to the workbench.

Session selection is stored under `harness.session`; the theme continues to use
the original `theme` key. The hook closes obsolete streams, aborts obsolete fetches,
and rejects late results from previously selected sessions. It replays all event
pages and deduplicates streamed events by ID. The persisted event log is used as
the conversation transcript to avoid a running worker changing history between
the snapshot's history and cursor reads. History is a fallback for sessions
without conversation events. Snapshot approvals remain authoritative for earlier
events, so approvals resolved or discarded by a service restart are not revived.

## Running and configuration

The Next.js project now lives in `interfaces/web/frontend/`, alongside the
Python HTTP/SSE adapter (`app.py`) and web host (`host.py`). The CLI remains in
`interfaces/cli.py`; `scripts/project.ps1` manages the entire repository.

Install the migrated frontend dependencies once:

```powershell
.\scripts\project.ps1 deps
```

On this Windows machine, this delegates to `install-deps.ps1`, which keeps this
project's packages at `D:\nvm\project-deps\agent-harness\node_modules` and
exposes them through a `node_modules` directory junction here. `package.json`
and `package-lock.json` in this source directory remain authoritative. Run
`.\interfaces\web\frontend\install-deps.ps1 -Refresh` from the repository root
to reinstall after changing the lockfile. Do not run a plain `npm install` or
`npm ci` in the source directory after setting up the junction: those commands
can replace it and recreate local packages. Running the npm scripts below is
supported; development and build scripts use webpack for external junction
compatibility. Node.js and npm themselves remain managed separately from these
project dependencies. Other machines may use a conventional local installation.

Production preserves the original static export workflow:

```powershell
$env:NEXT_TELEMETRY_DISABLED = '1'
npm --prefix interfaces/web/frontend run build
. .\.venv\Scripts\Activate.ps1
python -m agent_harness
```

The Python web server serves `interfaces/web/frontend/out` and the live API together. There is
no `next start` command because the production frontend is a static export.
`out/` contains this project's generated pages and browser assets, so it stays
with the application. It is not a library installation or an npm cache. Once
it has been built, the default Python static mode needs neither Node.js nor
`node_modules`; rebuilding and frontend development still require them.

For frontend development, start the Python API then run Next.js with a port:

```powershell
python -m agent_harness api --port 8000 --web-port 3000
```

In another shell, run:

```powershell
$env:AGENT_API_URL = 'http://127.0.0.1:8000'
npm --prefix interfaces/web/frontend run dev -- --port 3000
```

All browser requests use `/api/agent/*`. Development rewrites proxy them to
`${AGENT_API_URL}/api/*`; the production Python app serves the same prefix.
`NEXT_PUBLIC_TEACHING_URL` optionally points the retained course navigation to
a separately running copy of `web/` (for example `http://127.0.0.1:3001`). Its
default is the original project's public teaching site. The value is used when
Next builds the static output.

`python -m agent_harness --dev --port 8000 --web-port 3000` starts both
development services with matching configuration from a single command.

Pages are `/zh/`, `/en/`, and `/ja/`. `/` redirects to `/zh/`. Fonts use system
defaults, matching the original UI; no remote font download is required.

## Browser selectors

| Purpose | Selector |
| --- | --- |
| Service connection | `[data-testid="connection"]`, `#connection` |
| Create conversation | `#new-session` |
| Select conversation | `#sessions button[data-session-id="<id>"]` |
| Message input | `[data-testid="message-input"]`, `#message` |
| Send message | `[data-testid="send-message"]`, `#send` |
| Cancel active execution | `[data-testid="cancel-run"]` |
| Pending approval | `[data-testid="approval"]`, `.approval` |
| Approve / deny | `[data-testid="approval-allow"]` / `[data-testid="approval-deny"]` |
| Assistant response | `.message.assistant .body` |
| Execution status | `#runtime-status` |
| Task state | `#tasks` |
| Tool call count | `#tool-count` |
| Composer | `#composer` |

The original teaching page components belong to the original repository's `web/`,
which is not included in this standalone repository.
