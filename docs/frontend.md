# Frontend

React 18 + Vite + TypeScript SPA. Dev server on port 3000, production builds to `frontend/dist/`.

## Cole — The AI Persona

The assistant is named **Cole** — a warm, knowledgeable college admissions advisor who feels like a supportive older friend who just went through the process. Cole is not a faceless chatbot; he introduces himself, speaks in first person, and maintains a cheerful, encouraging, and real tone throughout the experience. He talks directly to the person he's helping — never referring to them as "the student" or "the user."

**How Cole appears in the UI:**

- **Avatar:** A forest-green circle with a bold white "C" (`ColeAvatar` component). Appears in the sidebar header (32px), welcome state (48px), message labels (20px), and mobile header.
- **Name label:** "Cole" in `text-forest-400` appears next to the avatar on every assistant message, streaming indicator, and sidebar header. The subtitle reads "Your college advisor."
- **Welcome state greeting:** "Hey, I'm Cole" with tagline: "Your friendly college advisor. Ask me about admissions, essays, scholarships, or deadlines."
- **Placeholder text** in the chat input references Cole by name: "Ask Cole about colleges..."
- **Loading states:** "Cole is thinking..." with pulsing dots while waiting for the first token, "Connecting to Cole..." during initial API health check
- **Sidebar hints** speak in Cole's voice: "Cole will use them as context when helping with essays."
- **College combobox** placeholder: "Select a school (Or just mention the school in your question, Cole will understand.)"

Cole's personality is embedded in both the UI copy and the backend system prompts (all LLM prompts open with the `COLE_PREAMBLE` which sets the warm, friendly persona and includes two explicit guardrails: never refer to the person as "the student" or "the user", and never reflexively open responses with filler like "Great question!" — warmth surfaces through word choice inside the answer, not through stock openers). This ensures the assistant feels consistent whether someone is reading a welcome message, watching a loading state, chatting, or scanning a placeholder.

## Tech Stack

React 18, TypeScript, Tailwind CSS, Zustand (state with localStorage persistence), Framer Motion (animations), Headless UI (combobox), react-markdown + remark-gfm + rehype-raw.

## Architecture

ChatGPT-style layout: persistent sidebar (280px) on the left, main content area on the right. The app has three modes:

| Mode | Key | View | Conversation-based? |
|---|---|---|---|
| Chat | `qa` | ChatView + InputArea | Yes (restores last conversation on tab switch) |
| Admissions Calculator | `admissions` | AdmissionsView | No (standalone form) |
| My Profile | `experiences` | ExperiencesView | No (standalone form) |

**Entry point** (`App.tsx`): Renders `Sidebar` + main content. Main content switches on `mode`: Chat renders `ChatView` + `InputArea`, Admissions renders `AdmissionsView`, Experiences renders `ExperiencesView`. An `ErrorBoundary` wraps the entire app. The `useApi` hook runs on mount to check `/health` and fetch `/options`.

## State Management

Zustand store (`store.ts`) with `persist` middleware, serializing to `localStorage` key `college-ai-store`.

**Persisted:**
- `conversations` — `Record<string, Conversation>`, max 50 with LRU eviction
- `conversationOrder` — `string[]` sorted by recency (newest first)
- `experiences` — `Experience[]` (user's extracurriculars)
- `profile` — `ProfileData` (GPA, test score type, test score, country, countryLabel, state, preferredMajors, savedSchools)
- `activeConversationId` — currently selected conversation
- `contextSize` — RAG context size (`'XS'|'S'|'M'|'L'|'XL'`, default `'M'`). Controls `top_k` sent to backend (3/5/8/12/16 sources)
- `responseLength` — Response length preference (`'XS'|'S'|'M'|'L'|'XL'`, default `'M'`). Controls LLM length budgets (XS: 50-100w, S: 100-200w, M: auto-detect, L: 400-600w, XL: 600-900w)

**Ephemeral (reset on reload):**
- `mode` — current `AppMode`
- `isConnected` — whether `/health` returned ok
- `collegeOptions` — college list from `/options` (sourced from `college_ai/scraping/colleges/colleges.csv` only)
- `schoolStates` — `Record<string, string>` mapping school names to state codes (from `/options`), used for auto-residency
- `streamingContent` — accumulated tokens during SSE streaming
- `streamingLoading` — whether a stream is in progress
- `sidebarOpen` — mobile sidebar visibility
- `pendingEdit` — `ChatMessage | null`, set when user clicks Edit on a message bubble; consumed by InputArea to populate fields

## Components

### Sidebar (`Sidebar.tsx`)

Fixed-width (280px) panel containing:
1. **Header:** Cole avatar (32px green circle with "C") + name + "Your college advisor" subtitle
2. **New Chat button:** Only visible in Chat mode. Creates a new conversation.
3. **Mode selector:** Vertical list of 3 buttons with emoji icons (💬 Chat, 🎯 Admissions, 📋 My Profile). Active mode gets a forest-green highlight with subtle border. Switching to Chat restores the most recent conversation; the welcome screen only appears when no conversations exist.
4. **Conversation list:** Only shown in Chat mode. Shows all chat conversations (including legacy essay-mode conversations). Each item shows the conversation title with a delete button on hover.
5. **Contextual hints:** In Admissions mode: "Add schools and see your estimated admission chances." In Experiences mode: "Add your activities, projects, and experiences. Cole will use them as context when helping with essays."

On mobile (`< lg` breakpoint): sidebar is `position: fixed`, slides in/out with spring animation (damping 25, stiffness 300). A semi-transparent backdrop overlay dismisses it on tap. Close button (X) appears in top-right.

### ChatView (`ChatView.tsx`)

Renders the conversation for Chat mode.

**WelcomeState** (no active conversation or empty messages):
- Large Cole avatar (48px)
- "Hey, I'm Cole" heading
- Unified tagline: "Your friendly college advisor. Ask me about admissions, essays, scholarships, or deadlines."
- 4 randomized suggestion chips from combined QA + essay suggestion pools (~150 total). `pickRandom()` shuffles Fisher-Yates style, memoized. Clicking a chip resets the active conversation and immediately submits the question via `useStreaming.send()`.

**Message list:**
- Scrollable area with `max-w-3xl` centered content
- Messages rendered via `MessageBubble` with `AnimatePresence` for enter animations
- Auto-scrolls to bottom on new messages and during streaming

**Streaming states:**
- `StreamingMessage`: Shows accumulated tokens with Cole avatar + name + pulsing green dots
- Loading indicator (before first token): "Cole is thinking..." with bouncing dots
- Both use `motion.div` with fade-in + slide-up animation

### MessageBubble (`MessageBubble.tsx`)

- **User messages:** Right-aligned, forest-600 background, white text, `rounded-2xl rounded-br-md` (chat bubble shape)
- **Assistant messages:** Full-width, left-aligned with Cole avatar (20px) + "Cole" label in forest-400. Content rendered as markdown (GFM + raw HTML). Confidence badge shown inline with Cole's name if present.
- **Action buttons:** Hover-revealed (`opacity-0 group-hover:opacity-100`) action buttons on each message. Styled as `w-7 h-7` rounded icon buttons with `bg-dark-800/80 border border-dark-700`.
  - **User messages:** Edit + Copy buttons appear BELOW the green bubble, right-aligned (`flex flex-col items-end`). Edit dispatches `setPendingEdit(message)` to the store (consumed by InputArea). Copy copies the question text only (essay prompt/draft not included).
  - **Assistant messages:** Copy button appears BELOW the response text, left-aligned. Fades in on `group-hover/msg` (message-level hover). Copy uses `stripMarkdown()` to produce clean plain text (citations and markdown syntax removed).
  - **Copy feedback:** Icon swaps to a green checkmark for 1.5 seconds after copying.
- **Draft loaded badge:** Clickable badge on user messages that had an essay draft. On hover: text animates from "Draft loaded" to "Display full essay draft" via `max-width` transition (200ms), background shifts to `bg-forest-500/30` with `border-forest-500/40`. Clicking opens a fixed modal overlay displaying the full essay draft text. Modal: centered `max-w-2xl` card in `bg-dark-900`, `border-dark-700`, `rounded-2xl` with scrollable content area. Dismisses via X button or clicking the `bg-black/60` backdrop.
- **Sources toggle:** When sources exist, a green "Show Sources" pill button appears in the top-right of the header row (`ml-auto`). Clicking toggles to "Hide Sources" (same style). Sources are hidden by default.
  - **Hidden state:** `[N]` citation markers are stripped via `processOfficialCitations()`, but `[SD]` (school data) markers are still rendered as green checkmark badges (`source-badge-official`). No source cards visible.
  - **Shown state:** `[N]` markers are converted to gray badge elements via `processCitations()` (class `source-badge`, rendered through `rehype-raw`). `[SD]` markers render as green checkmark badges. Source cards appear below with AnimatePresence height animation. All sources shown (no limit).
  - **Official source badges (`[SD]`):** Always visible regardless of toggle state. Clickable green circle with white checkmark (`source-badge-official`) — rendered as an `<a>` tag linking to `https://collegescorecard.ed.gov/` (opens in new tab). On hover: lighter green + "Official Source" tooltip (CSS-only, `official-tooltip`). Sentence underline highlight works the same as numbered badges.
- **Citation badge interaction** (event delegation via `mouseover`/`mouseout`/`click` on container ref):
  - **Hover:** Badge turns green (`source-badge--active`). The sentence immediately before the hovered badge gets a dotted green underline via the CSS Custom Highlight API (`getSentenceRange()` walks backwards through DOM siblings to find the preceding text, `CSS.highlights` applies `::highlight(source-hl)`). Only the hovered badge's sentence is underlined — same source number at different locations does not cross-highlight. Falls back to block-level `.source-highlight` class if the Highlight API is unavailable.
  - **Click:** Smooth scrolls to the corresponding `SourceCard` below (`scrollIntoView({ block: 'center' })`), briefly highlights it with a green ring (1.5s)

### SourceCard (`SourceCard.tsx`)

Expandable card with forest-green left border (`border-l-4 border-l-forest-500`). Only visible when "Show Sources" is toggled on in the parent MessageBubble:
- Numbered source badge (matching inline citation style, `source-badge--static` green variant) before college name
- College name badge (forest green pill)
- Page type label
- Clickable title linking to source URL
- Content preview (first 200 chars, click to expand full content)
- Staggered entrance animation (50ms delay per card)
- Has `id={source-${index}}` for citation badge click-to-scroll targeting

### InputArea (`InputArea.tsx`)

Pinned to bottom of chat, `border-t border-dark-700` with backdrop blur.

**Layout:**
1. **ReviewPanel:** Always available via toggle button above the main input area. Contains essay prompt input and essay draft textarea.
2. **School selection row:**
   - College combobox (full width)
   - Info tooltip (when no college selected) or "See my chances" button (when college selected → opens QuickPredictModal)
3. **Chat input row:**
   - Auto-resizing textarea (max 150px height), Enter to submit, Shift+Enter for newline
   - Settings popover (bottom-right of textarea): Headless UI `Popover` with settings gear icon + chevron. Opens upward with two sections — **Context Size** (XS/S/M/L/XL controlling `top_k`) and **Response Length** (XS/S/M/L/XL controlling LLM length budget). Both use pill-button selectors with forest-green active state. Persisted across sessions.
   - Send button (forest-600 circle) or Cancel button (red circle during streaming)

**Connecting state:** Full skeleton UI with pulsing placeholder blocks + "Connecting to Cole..." label with bouncing dots. Shown while `isConnected` is false.

**Edit message handling:** InputArea watches `pendingEdit` from the store. When set (user clicked Edit on a message bubble), it: (1) cancels streaming if active, (2) populates the textarea with the original question, (3) restores the essay prompt to the conversation, (4) restores the essay draft text and auto-opens the ReviewPanel via `forceOpen` prop, (5) focuses the textarea. Messages are not deleted — the user re-sends as a new message.

**Validation:** Can't send without non-empty input and connection. If essay text is provided, essay prompt is required (prompt field flashes red if missing). Disabled during streaming.

### ReviewPanel (`ReviewPanel.tsx`)

Collapsible essay draft editor, available in all chat conversations:
- Toggle button: "Essay Help" / "Hide Essay" pill with rotating chevron. Glows green when content exists.
- Slides up to 280px with spring animation
- Accepts `forceOpen` prop — when true, auto-opens the panel (used by edit message flow to reveal restored essay data)
- **Essay prompt input** at top (same `input-field-compact` style as other inputs). Placeholder: "Essay prompt (leave blank for general advice)". Flashes red with warning placeholder when user tries to send with essay text but no prompt.
- Header bar: "Your Essay Draft" + word count
- Bordered textarea for pasting essay content (`border border-dark-600 rounded-lg`)
- When essay text is present → backend auto-classifies as `essay_review`
- When only essay prompt is present (no text) → backend auto-classifies as `essay_ideas`
- Both can be left blank for normal Q&A behavior

### QuickPredictModal (`QuickPredictModal.tsx`)

Modal overlay (`max-w-lg`) for quick admission prediction within a chat:
- Header: "Quickly estimate admissions probability" (white text, `text-sm`)
- Row 1: GPA input (0–4.0, fixed `w-24`)
- Row 2: SAT/ACT toggle + score input
- Row 3: Major dropdown + Residency selector (Not specified, In-State, Out-of-State, International) — equal width (`flex-1`)
- Number input spinners hidden via CSS
- Auto-populates from profile data if available
- Calls `POST /predict` and displays a `PredictionCard` inline

### ExperiencesView (`ExperiencesView.tsx`)

Standalone view for My Profile mode:

**Academic Info card:**
- GPA input (0–4.0 with validation)
- SAT/ACT toggle (two buttons with active highlight)
- Test score input (400–1600 for SAT, 1–36 for ACT)
- Location: country dropdown (all countries, US first) + conditional US state dropdown. Selecting a non-US country clears the state. Location data drives auto-residency in AdmissionsView: the `/options` endpoint fuzzy-matches school names (from `college_ai/scraping/colleges/colleges.csv`) against the Turso DB to build a school→state mapping, enabling `computeResidency()` to determine in-state/out-of-state/international residency per school
- All values persisted to Zustand `profile`, auto-populate Admissions Calculator and QuickPredictModal
- Profile data is also sent to the backend on every streaming request (all modes) for stats contextualization, residency-aware tuition advice, and major-specific guidance via `format_profile_context()` on backend

**Major Preferences card:**
- Searchable Headless UI `Combobox` to add majors from `ALLOWED_MAJORS` (filters out already-selected)
- **Limit: 15 majors.** Count displayed in heading as `(X/15)`. Exceeding the limit triggers a red drop shadow flash on the card (`shadow-[0_0_16px_rgba(239,68,68,0.5)]`, fades over 500ms via `transition-shadow`)
- Drag-to-reorder ranked list using Framer Motion `Reorder.Group` / `Reorder.Item`
- Each item shows: rank number (#1, #2, ...), drag handle (grip dots), major name, remove button (X)
- Persisted in `profile.preferredMajors: string[]` (ordered by preference)
- Passed to LLM as `"Preferred majors (ranked): #1 Computer Science, #2 Data Science"` via `format_profile_context()`

**Saved Schools card:**
- Searchable `CollegeCombobox` (with `showDefaultScreen={false}`, `excludeValues={savedSchools}`) to add schools the user is interested in — already-saved schools are filtered out of the dropdown
- **Limit: 25 schools.** Count displayed in heading as `(X/25)`. Exceeding the limit triggers a red drop shadow flash (same effect as majors)
- Drag-to-reorder ranked list using Framer Motion `Reorder.Group` / `Reorder.Item` (same UI as Major Preferences)
- Each item shows: rank number (#1, #2, ...), drag handle (grip dots), school name, remove button (X)
- Persisted in `profile.savedSchools: string[]` (ordered by preference)
- Passed to LLM as `"Preferred schools (ranked): #1 MIT, #2 Stanford"` via `format_profile_context()`, with a note that rankings are subject to change
- Saved schools appear as a "My Schools" section at the top of all CollegeCombobox dropdowns in chat tabs (Q&A, Essay, Admissions) — the profile's own school picker does not show this sectioned view

**Experiences card:**
- Dedicated section card (same style as Academic Info / Major Preferences / Saved Schools)
- Section header with "Experiences" title, subtitle, and inline "Add" button (right-aligned)
- Experience items rendered as `bg-dark-800 rounded-lg` rows inside the card
- Each item shows: title, type badge (color-coded), organization, dates, truncated description
- Edit/delete buttons appear on hover (opacity transition)
- Empty state: clipboard emoji + "No experiences yet" (compact, inside the card)

**Type badges** (color-coded pills):
- Extracurricular: teal (`bg-teal-500/15 text-teal-400`)
- Project: sky blue (`bg-sky-500/15 text-sky-400`)
- Work: amber (`bg-amber-500/15 text-amber-400`)
- Volunteer: purple (`bg-purple-500/15 text-purple-400`)

### ExperienceForm (`ExperienceForm.tsx`)

Modal for adding/editing experiences:
- Title, organization, type dropdown (4 options), description textarea
- Start date + end date (month pickers), "Present" checkbox for ongoing
- Create or edit mode (pre-fills fields when editing)

### AdmissionsView (`AdmissionsView.tsx`)

Standalone view for Admissions Calculator mode:

**Stats card:**
- Same GPA + SAT/ACT inputs as ExperiencesView (shared profile data)
- Default Major: searchable `MajorCombobox` (Headless UI Combobox). When search is empty, shows sectioned default menu — "Your Majors" (from `profile.preferredMajors`, alphabetized) then "All Majors" (remaining, alphabetized). When search has text, shows flat alphabetized filtered results
- Default Residency selector: always editable dropdown with options: Use Location (when eligible), Not specified, In-State, Out-of-State, International. "Use Location" is a special mode that auto-computes residency per school from profile location when adding schools.
- Required fields marked with asterisk

**School picker:**
- CollegeCombobox for adding schools — the dropdown does **not** filter out already-selected schools, so the same school can be added multiple times (e.g. to compare the same school under different residency/major settings). Each selection is tracked as an independent entry with its own `crypto.randomUUID()` id, so remove/update handlers and the per-school results map key by `id`, not `name`.
- "Add saved schools" button (top-right, `phase === 'idle'` only): visible whenever the user has any saved schools **and** at least one open slot (`selectedSchools.length < MAX_SCHOOLS`). It does not hide as saved schools get added — clicking again appends the full saved-schools list (up to the remaining slot budget) using the current Default Residency + Default Major, so each click produces a fresh batch of entries.
- Counter: "Schools (N/10)"
- Selected schools shown in alternating-row list with school name, searchable major combobox (`MajorCombobox compact`, `w-28` input with wider `w-56` dropdown), and residency dropdown (`w-28`)
- Max 10 schools

**Auto-residency:** `computeResidency()` compares profile location to school state (from `/options` `school_states` mapping, fuzzy-matched via rapidfuzz against the Turso DB). Non-US country → "International". US with matching state → "In-State". US with different state → "Out-of-State". No match found → null (no residency).

- **Location-eligible** means: country is set AND (country is non-US OR US with state selected). US without state → not eligible.
- **Default Residency dropdown:** When location-eligible, defaults to "Use Location" on every tab load (auto-computes per school). User can switch to a manual option and switch back. "Use Location" option is only visible when eligible. Only the main Default Residency dropdown has this option — per-school and QuickPredictModal dropdowns do not.
- **Per-school residency dropdowns:** Always editable with options: No residency, In-State, Out-of-State, International. Pre-populated from default residency when the school is added, but user can always override.
- **On submit:** Uses each school's selected residency value directly — no recomputation. User overrides are respected.

**Calculate button:** Full-width forest-600 button with bouncing dots during loading

**Results:** List of `PredictionCard` components with staggered animation

### PredictionCard (`PredictionCard.tsx`)

Displays a single school's admission prediction:
- School name + acceptance rate
- Large probability percentage (color-coded by classification)
- Classification badge: Safety (green), Match (amber), Reach (red)
- 95% confidence interval
- Contributing factors list (positive/negative with impact details)

### CollegeCombobox (`CollegeCombobox.tsx`)

Headless UI Combobox with `immediate` (auto-opens on focus). Renders static (non-virtualized) `ComboboxOption` children:
- **Render cap (`MAX_RESULTS = 100`):** at most 100 option rows are rendered at any time in both browse and search views. This bounds mount/unmount cost — rendering every one of the ~6,500 schools caused a noticeable close-lag (~100–300ms) from synchronous `useEffect` cleanup + DOM teardown. Search still runs over the full `collegeOptions` list, so every school is reachable by typing any part of its name; only the rendered slice is capped. When the underlying list exceeds the cap, a muted italic `<div>` appears at the bottom of the dropdown:
  - Browse view: `"Type to search for more schools."`
  - Search view: `"Showing first 100 matches. Keep typing to refine."`
- **Default screen** (`showDefaultScreen` prop, default `true`): when query is empty, shows a sectioned view — "My Schools" (from `profile.savedSchools`, alphabetized) followed by "All Schools" (remaining schools, alphabetized, filling the remaining budget up to `MAX_RESULTS - validSavedSchools.length` items). Section headers are non-interactive `div` labels matching `MajorCombobox` styling. Saved schools that no longer exist in `collegeOptions` are filtered out. A static "Not Specified" row (value: null) sits at the very top.
- **Flat mode** (`showDefaultScreen={false}`): used only by the Profile tab's Saved Schools picker. Mirrors how the inline Major selector in the same card works — no "Not Specified" row, no section headers, just a flat alphabetical list of schools with `excludeValues` applied, still capped at 100 with the same "Type to search for more schools." hint.
- **Search mode** (query entered, both default and flat modes): flat list of up to `MAX_RESULTS` matches from `collegeOptions`, case-insensitive, sorted alphabetically. Section headers are suppressed in search mode even with `showDefaultScreen={true}` since sections only help when browsing.
- **Alphabetical ordering:** both sections and all filtered lists are sorted via `localeCompare` with `{ sensitivity: 'base' }` on `formatSchoolName(c)`, so display order is case- and diacritic-insensitive.
- **`excludeValues` prop** (optional `string[]`): filters specified schools out of all views. Used by Profile Saved Schools (excludes already-saved schools, which makes the "My Schools" section empty and collapses to flat mode behavior). AdmissionsView deliberately does **not** pass this prop — duplicate selections are allowed there.
- **Clear (×) button:** when the input has a value, a small × button is rendered in the input's right gutter (between the chevron and content) that clears the selection to `null` on click. This replaces the need for a selectable "clear selection" row inside the dropdown.
- Compact variant (smaller padding/height) for inline use.
- Loaded from `/options` endpoint with 31-school fallback list.
- **Display-only name formatting:** option labels are wrapped in `formatSchoolName()` from `lib/format.ts`, which replaces the literal substring ` A and M ` with ` A&M ` so e.g. "Texas A and M University" renders as "Texas A&M University". The raw string is kept as the Combobox `value`/`key`, the Zustand `collegeOptions`/`savedSchools` arrays, and every outgoing API payload — the transform is purely visual. Same helper is applied in `AdmissionsView` (selected-school card + `title` tooltip), `ExperiencesView` (Profile → Saved Schools list), `PredictionCard` (result header + error row), and `SourceCard` (college badge). Backend school lookups (Turso/Zilliz) depend on the original string, so never persist or send the formatted form.

### MajorCombobox (`MajorCombobox.tsx`)

Searchable Headless UI Combobox with `immediate` (auto-opens on focus), used in AdmissionsView (default major + per-school cards):
- **Default menu** (empty search): two sections — "Your Majors" (from `profile.preferredMajors`, alphabetized) then "All Majors" (remaining `ALLOWED_MAJORS`, alphabetized). Section headers are non-interactive `div` labels. Both sections use `localeCompare` with `{ sensitivity: 'base' }` for case-insensitive ordering. Note the display order in the dropdown is alphabetical — this is independent of the user's ranked order in `profile.preferredMajors`, which is still used downstream for LLM context.
- **Search mode** (query entered): flat alphabetized filtered list from all `ALLOWED_MAJORS`, case-insensitive
- "Not specified" / "No major" clear option (value: null) always at top
- No render cap needed — `ALLOWED_MAJORS` only has ~51 entries so mount/unmount cost is negligible
- **Compact mode** (`compact` prop): `text-xs py-1.5 w-28` input with wider `w-56` dropdown anchored bottom-end. Used in per-school cards
- **Full mode**: `text-sm` input, dropdown matches parent width. Used for default major

The Profile tab has a separate **inline** major selector (not `MajorCombobox`) rendered directly inside `ExperiencesView.tsx` for the "Major Preferences" card. It uses the same alphabetized-filtered pattern over `ALLOWED_MAJORS` minus already-saved majors, and mirrors the Profile tab's flat-mode `CollegeCombobox` behavior.

### ConfidenceBadge (`ConfidenceBadge.tsx`)

Small pill badge showing confidence level (high: green, medium: amber, low: red). Expands on **message hover** (`group-hover/msg`) to reveal the label text and hint — the parent `MessageBubble` assistant container carries the `group/msg` class.

### ConversationList (`ConversationList.tsx`)

Sidebar conversation history showing all chat conversations (both current `qa` and legacy `essay` mode):
- Sorted by `conversationOrder` (recency)
- Active conversation highlighted
- Delete button on hover
- Click to switch active conversation

## API Layer (`api.ts`)

**Base URL:** `window.COLLEGE_AI_API_URL || 'https://api.mommy-soul.com'`

**Functions:**
- `checkHealth()` → `GET /health`
- `getOptions()` → `GET /options` → `{colleges, school_states}` (fallback: 31 hardcoded colleges, empty states)
- `ask(params)` → `POST /ask` (non-streaming, not used by main UI)
- `askStream(params, callbacks, signal)` → `POST /ask/stream` (SSE streaming)
- `predict(params)` → `POST /predict`
- `compare(params)` → `POST /compare`

**SSE parsing:** `askStream` uses `ReadableStream` reader with manual `data:` line parsing. Dispatches to `StreamCallbacks`: `onToken`, `onSources`, `onDone`, `onError`.

**Source normalization:** Backend may return sources with nested `entity` sub-objects (from Milvus ORM hits). `normalizeSources()` flattens these before passing to components.

## Hooks

### `useApi` (`hooks/useApi.ts`)

One-time mount effect: calls `checkHealth()` and `getOptions()` in parallel. Sets `isConnected`, `collegeOptions`, and `schoolStates` in store.

### `useStreaming` (`hooks/useStreaming.ts`)

Returns `{ send, cancel }`:

- **`send(question, essayText?)`:** Creates conversation if needed, adds user message (including `essayPrompt`, `essayText`, and `hasEssayDraft` fields for later edit restoration), builds request (with history, experiences, college, essay_prompt, essay_text, profile, `top_k` from `contextSize`, `response_length` from `responseLength`), initiates SSE stream via `askStream`. Collects tokens into `streamingContent`, then on `onDone` assembles final assistant message with sources/confidence and adds to conversation.
- **`cancel()`:** Aborts the AbortController, clears streaming state.

History is built from the last 6 messages of the current conversation. Experiences and profile data are always included when available (not mode-gated). Essay prompt and essay text are sent when present in the ReviewPanel. Profile data (GPA, test scores, location, preferred majors, saved schools) is sent on every request when a GPA has been entered, a country set, preferred majors added, or schools saved.

## Conversations

- Max 50, LRU eviction (oldest removed when creating new conversation at limit)
- Auto-titled from first user message (truncated at 60 chars)
- All new conversations created as `'qa'` mode (legacy `'essay'` mode conversations from before the consolidation are still displayed)
- Each conversation stores: selected `college`, `essayPrompt`, `messages[]`, timestamps
- Sidebar shows all chat conversations together

## Design System

### Colors (from `tailwind.config.ts`)

**Dark palette:**
| Token | Hex | Usage |
|---|---|---|
| `dark-950` | `#0f0f12` | App background |
| `dark-900` | `#18181b` | Surface (sidebar, cards) |
| `dark-800` | `#222226` | Elevated surfaces (inputs, buttons) |
| `dark-700` | `#2e2e33` | Borders, dividers |

**Forest (accent):**
| Token | Hex | Usage |
|---|---|---|
| `forest-700` | `#075831` | Darkest accent |
| `forest-600` | `#096E3D` | Primary buttons, Cole avatar, active states |
| `forest-500` | `#108C4E` | Focus rings, source card borders |
| `forest-400` | `#34B874` | Text accents, Cole name, streaming dots |
| `forest-300` | `#6ED2A0` | Active mode labels |

**Text:** slate-100 (primary), slate-200 (headings), slate-300 (body), slate-400 (secondary), slate-500 (muted/timestamps/placeholders), slate-600 (hints)

**Experience type colors:** teal (extracurricular), sky (project), amber (work), purple (volunteer)

**Classification colors:** green (safety), amber (match), red (reach)

### Typography

- **Font:** Inter → system-ui → -apple-system → sans-serif
- **Sizes:** text-lg (section headings), text-base (sidebar name), text-sm (body, messages, inputs), text-xs (labels, badges, metadata), text-[10px] (subtitles, avatar letters)

### Shapes

- `rounded-2xl` — cards, message bubbles
- `rounded-xl` — inputs, textareas
- `rounded-lg` — buttons, mode selectors, chips
- `rounded-full` — avatars, badges, pills, send button, suggestion chips

### Shadows

Custom dark shadow scale tuned for dark UI (higher opacity than Tailwind defaults):
- `dark-sm`: `0 1px 2px rgba(0,0,0,0.3)`
- `dark`: `0 1px 3px rgba(0,0,0,0.4), 0 1px 2px -1px rgba(0,0,0,0.3)`
- `dark-md`: `0 4px 6px -1px rgba(0,0,0,0.4), 0 2px 4px -2px rgba(0,0,0,0.3)`
- `dark-lg`: `0 10px 15px -3px rgba(0,0,0,0.4), 0 4px 6px -4px rgba(0,0,0,0.3)`

### Animations

- `bounce-dot`: Three-dot loading indicator (scale 0 → 1 → 0)
- `fade-in`: 0.3s ease-out opacity transition
- `slide-up`: 0.4s ease-out translateY(10px) → 0 with opacity
- `pulse-soft`: 2s infinite opacity 1 → 0.6 → 1
- Framer Motion springs: damping 25, stiffness 300 (sidebar slide, review panel)
- Framer Motion entrance: `initial={{ opacity: 0, y: 8 }}` (messages, cards)

## Mobile UX

- Sidebar is `fixed` below `lg` breakpoint (1024px), slides in/out with spring animation
- Hamburger icon in mobile header (only visible when sidebar is closed)
- "Cole" label in mobile header for branding continuity
- Semi-transparent black backdrop overlay (`bg-black/50`) behind sidebar on mobile, tap to dismiss
- Close button (X) inside sidebar on mobile
- All content areas scroll independently via `overflow-y-auto` with `custom-scrollbar`

## Suggestions (`suggestions.ts`)

- `QA_SUGGESTIONS`: ~100 sample questions across categories (admissions, deadlines, financial aid, academics, campus life, career outcomes, international, transfer, safety, diversity, comparisons, chance-me, strategy)
- `ESSAY_SUGGESTIONS`: ~50 prompts across categories (school-specific brainstorming, general brainstorming, topics, review, strategy/structure)
- `pickRandom(list, n)`: Fisher-Yates shuffle, returns n items without repetition
- Welcome state renders 4 suggestions, memoized per mode to avoid reshuffling on re-render

## File Structure

```
frontend/src/
├── App.tsx                    Entry point, mode routing
├── api.ts                     API client (REST + SSE streaming)
├── store.ts                   Zustand store (persisted + ephemeral)
├── types.ts                   TypeScript types, ALLOWED_MAJORS, ContextSize, CONTEXT_SIZE_MAP
├── suggestions.ts             QA + Essay suggestion banks
├── markdown.tsx               Citation + markdown utilities (processCitations, processOfficialCitations, stripCitations, stripMarkdown)
├── data/
│   └── locations.ts           Country + US state dropdown options
├── lib/
│   └── format.ts              Display-only string helpers (formatSchoolName: " A and M " → " A&M ")
├── hooks/
│   ├── useApi.ts              Health check + options fetch on mount
│   └── useStreaming.ts        SSE streaming hook (send/cancel)
└── components/
    ├── Sidebar.tsx            Mode selector + conversation list
    ├── ChatView.tsx           Message list + welcome state + streaming
    ├── MessageBubble.tsx      User/assistant message rendering
    ├── InputArea.tsx          Chat input + college picker + mode fields
    ├── CollegeCombobox.tsx    Searchable school dropdown (Headless UI)
    ├── MajorCombobox.tsx      Searchable major dropdown with preferred sections
    ├── ReviewPanel.tsx        Collapsible essay draft editor
    ├── QuickPredictModal.tsx  Inline admission prediction modal
    ├── SourceCard.tsx         Expandable source citation card
    ├── ConfidenceBadge.tsx    High/medium/low confidence pill
    ├── ConversationList.tsx   Sidebar conversation history
    ├── ExperiencesView.tsx    Profile + experience CRUD
    ├── ExperienceForm.tsx     Add/edit experience modal
    ├── AdmissionsView.tsx     Batch admission calculator
    ├── PredictionCard.tsx     Single school prediction result
    ├── LoadingState.tsx       Full-page loading skeleton
    └── ErrorBoundary.tsx      React error boundary wrapper
```
