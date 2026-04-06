# Frontend

React 18 + Vite + TypeScript SPA. Dev server on port 3000, production builds to `frontend/dist/`.

## Cole — The AI Persona

The assistant is named **Cole** — a friendly, knowledgeable college advisor who is always by the user's side. Cole is not a faceless chatbot; he introduces himself, speaks in first person, and maintains a warm, encouraging tone throughout the experience.

**How Cole appears in the UI:**

- **Avatar:** A forest-green circle with a bold white "C" (`ColeAvatar` component). Appears in the sidebar header (32px), welcome state (48px), message labels (20px), and mobile header.
- **Name label:** "Cole" in `text-forest-400` appears next to the avatar on every assistant message, streaming indicator, and sidebar header. The subtitle reads "Your college advisor."
- **Welcome state greeting:** "Hey, I'm Cole" with mode-specific taglines:
  - Q&A: "Your friendly college advisor. Ask me about admissions, requirements, scholarships, or deadlines."
  - Essay: "I'm your essay coach. I'll help you brainstorm ideas and review drafts using real college data."
- **Placeholder text** in the chat input references Cole by name: "Ask Cole about colleges..." (Q&A) and "Tell Cole what to focus on..." (Essay)
- **Loading states:** "Cole is thinking..." with pulsing dots while waiting for the first token, "Connecting to Cole..." during initial API health check
- **Sidebar hints** speak in Cole's voice: "Cole will use them as context when helping with essays."
- **College combobox** placeholder: "Select a school (Optional, Cole will also understand if you just mention the school's name in your question)"

Cole's personality is embedded in both the UI copy and the backend system prompts (all LLM prompts open with "You are Cole, ..."). This ensures the assistant feels consistent whether the user is reading a welcome message, watching a loading state, chatting, or scanning a placeholder.

## Tech Stack

React 18, TypeScript, Tailwind CSS, Zustand (state with localStorage persistence), Framer Motion (animations), Headless UI (combobox), react-markdown + remark-gfm + rehype-raw.

## Architecture

ChatGPT-style layout: persistent sidebar (280px) on the left, main content area on the right. The app has four modes:

| Mode | Key | View | Conversation-based? |
|---|---|---|---|
| Q&A | `qa` | ChatView + InputArea | Yes |
| Essay Helper | `essay` | ChatView + InputArea | Yes |
| Admissions Calculator | `admissions` | AdmissionsView | No (standalone form) |
| My Profile | `experiences` | ExperiencesView | No (standalone form) |

**Entry point** (`App.tsx`): Renders `Sidebar` + main content. Main content switches on `mode`: Q&A/Essay render `ChatView` + `InputArea`, Admissions renders `AdmissionsView`, Experiences renders `ExperiencesView`. An `ErrorBoundary` wraps the entire app. The `useApi` hook runs on mount to check `/health` and fetch `/options`.

## State Management

Zustand store (`store.ts`) with `persist` middleware, serializing to `localStorage` key `college-ai-store`.

**Persisted:**
- `conversations` — `Record<string, Conversation>`, max 50 with LRU eviction
- `conversationOrder` — `string[]` sorted by recency (newest first)
- `experiences` — `Experience[]` (user's extracurriculars)
- `profile` — `ProfileData` (GPA, test score type, test score)
- `activeConversationId` — currently selected conversation
- `contextSize` — RAG context size (`'XS'|'S'|'M'|'L'|'XL'`, default `'M'`). Controls `top_k` sent to backend (3/5/8/12/16 sources)
- `responseLength` — Response length preference (`'XS'|'S'|'M'|'L'|'XL'`, default `'M'`). Controls LLM length budgets (XS: 50-100w, S: 100-200w, M: auto-detect, L: 400-600w, XL: 600-900w)

**Ephemeral (reset on reload):**
- `mode` — current `AppMode`
- `isConnected` — whether `/health` returned ok
- `collegeOptions` — college list from `/options`
- `streamingContent` — accumulated tokens during SSE streaming
- `streamingLoading` — whether a stream is in progress
- `sidebarOpen` — mobile sidebar visibility

## Components

### Sidebar (`Sidebar.tsx`)

Fixed-width (280px) panel containing:
1. **Header:** Cole avatar (32px green circle with "C") + name + "Your college advisor" subtitle
2. **New Chat button:** Only visible in Q&A/Essay modes. Creates a new conversation in the current mode.
3. **Mode selector:** Vertical list of 4 buttons with emoji icons (💬 Q&A, ✍️ Essay Helper, 🎯 Admissions, 📋 My Profile). Active mode gets a forest-green highlight with subtle border.
4. **Conversation list:** Only shown in Q&A/Essay modes. Filtered to current mode. Each item shows the conversation title with a delete button on hover.
5. **Contextual hints:** In Admissions mode: "Add schools and see your estimated admission chances." In Experiences mode: "Add your activities, projects, and experiences. Cole will use them as context when helping with essays."

On mobile (`< lg` breakpoint): sidebar is `position: fixed`, slides in/out with spring animation (damping 25, stiffness 300). A semi-transparent backdrop overlay dismisses it on tap. Close button (X) appears in top-right.

### ChatView (`ChatView.tsx`)

Renders the conversation for Q&A and Essay modes.

**WelcomeState** (no active conversation or empty messages):
- Large Cole avatar (48px)
- "Hey, I'm Cole" heading
- Mode-specific subtitle
- 4 randomized suggestion chips from `suggestions.ts` (~100 QA, ~50 Essay). `pickRandom()` shuffles Fisher-Yates style, memoized per mode. Clicking a chip resets the active conversation (clearing any stale college/essay prompt fields) and immediately submits the question via `useStreaming.send()` — one-click from welcome screen to streaming response.

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
- **Sources:** Shown below assistant messages, first 3 only. Each is a `SourceCard`.

### SourceCard (`SourceCard.tsx`)

Expandable card with forest-green left border (`border-l-4 border-l-forest-500`):
- College name badge (forest green pill)
- Page type label
- Clickable title linking to source URL
- Content preview (first 200 chars, click to expand full content)
- Staggered entrance animation (50ms delay per card)

### InputArea (`InputArea.tsx`)

Pinned to bottom of chat, `border-t border-dark-700` with backdrop blur.

**Layout:**
1. **ReviewPanel** (Essay mode only): Positioned above the main input area
2. **Mode-specific fields row:**
   - Essay prompt input (Essay mode only, required before sending)
   - College combobox (always shown, 2/5 width in essay, full width in Q&A)
   - "See my chances" button — inline to the right of school dropdown (appears when college selected → opens QuickPredictModal). When no college selected, a `w-9` spacer aligns the dropdown right edge with the chat textarea below
3. **Chat input row:**
   - Auto-resizing textarea (max 150px height), Enter to submit, Shift+Enter for newline
   - Settings popover (bottom-right of textarea): Headless UI `Popover` with settings gear icon + chevron. Opens upward with two sections — **Context Size** (XS/S/M/L/XL controlling `top_k`) and **Response Length** (XS/S/M/L/XL controlling LLM length budget). Both use pill-button selectors with forest-green active state. Persisted across sessions.
   - Send button (forest-600 circle) or Cancel button (red circle during streaming)

**Connecting state:** Full skeleton UI with pulsing placeholder blocks + "Connecting to Cole..." label with bouncing dots. Shown while `isConnected` is false.

**Validation:** Can't send without: non-empty input, connection, essay prompt (if essay mode). Disabled during streaming.

### ReviewPanel (`ReviewPanel.tsx`)

Collapsible essay draft editor in Essay mode:
- Toggle button: "Review Draft" / "Hide Draft" pill with rotating chevron
- Slides up to 220px with spring animation
- Header bar: "Your Essay Draft" + word count
- Full textarea for pasting essay content
- Essay text sent as `essay_text` in the streaming request for review feedback

### QuickPredictModal (`QuickPredictModal.tsx`)

Modal overlay (`max-w-lg`) for quick admission prediction within a chat:
- Header: "Quickly estimate admissions probability" (white text, `text-sm`)
- Row 1: GPA input (0–5.0, fixed `w-24`)
- Row 2: SAT/ACT toggle + score input
- Row 3: Major dropdown + Residency selector — equal width (`flex-1`)
- Number input spinners hidden via CSS
- Auto-populates from profile data if available
- Calls `POST /predict` and displays a `PredictionCard` inline

### ExperiencesView (`ExperiencesView.tsx`)

Standalone view for My Profile mode:

**Academic Info card:**
- GPA input (0–5.0 with validation)
- SAT/ACT toggle (two buttons with active highlight)
- Test score input (400–1600 for SAT, 1–36 for ACT)
- All values persisted to Zustand `profile`, auto-populate Admissions Calculator and QuickPredictModal
- Profile data is also sent to the backend on every streaming request (all modes) for stats contextualization via `format_profile_context()` on backend

**Experiences list:**
- Cards with title, organization, type badge (color-coded), dates, truncated description
- Edit/delete buttons appear on hover (opacity transition)
- Empty state: clipboard emoji + "No experiences yet" + instructional copy
- "Add" button in header opens ExperienceForm modal

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
- Major dropdown (83 options) + Residency selector
- Required fields marked with asterisk

**School picker:**
- CollegeCombobox for adding schools
- Counter: "Schools (N/10)"
- Selected schools as removable chips (rounded-full, dark-800 background, X button)
- Max 10 schools

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

Headless UI Combobox with:
- Case-insensitive filtering, first 50 results shown
- "All colleges" option (selects null)
- Compact variant (smaller padding/height) for inline use
- Loaded from `/options` endpoint with 31-school fallback list
- Placeholder references Cole by name

### ConfidenceBadge (`ConfidenceBadge.tsx`)

Small pill badge showing confidence level:
- High: green
- Medium: amber
- Low: red

### ConversationList (`ConversationList.tsx`)

Sidebar conversation history filtered by current mode:
- Sorted by `conversationOrder` (recency)
- Active conversation highlighted
- Delete button on hover
- Click to switch active conversation

## API Layer (`api.ts`)

**Base URL:** `window.COLLEGE_AI_API_URL || 'https://api.mommy-soul.com'`

**Functions:**
- `checkHealth()` → `GET /health`
- `getOptions()` → `GET /options` (fallback: 31 hardcoded colleges)
- `ask(params)` → `POST /ask` (non-streaming, not used by main UI)
- `askStream(params, callbacks, signal)` → `POST /ask/stream` (SSE streaming)
- `predict(params)` → `POST /predict`
- `compare(params)` → `POST /compare`

**SSE parsing:** `askStream` uses `ReadableStream` reader with manual `data:` line parsing. Dispatches to `StreamCallbacks`: `onToken`, `onSources`, `onDone`, `onError`.

**Source normalization:** Backend may return sources with nested `entity` sub-objects (from Milvus ORM hits). `normalizeSources()` flattens these before passing to components.

## Hooks

### `useApi` (`hooks/useApi.ts`)

One-time mount effect: calls `checkHealth()` and `getOptions()` in parallel. Sets `isConnected` and `collegeOptions` in store.

### `useStreaming` (`hooks/useStreaming.ts`)

Returns `{ send, cancel }`:

- **`send(question, essayText?)`:** Creates conversation if needed, adds user message, builds request (with history, experiences, college, essay_prompt, profile, `top_k` from `contextSize`, `response_length` from `responseLength`), initiates SSE stream via `askStream`. Collects tokens into `streamingContent`, then on `onDone` assembles final assistant message with sources/confidence and adds to conversation.
- **`cancel()`:** Aborts the AbortController, clears streaming state.

History is built from the last 6 messages of the current conversation. Experiences are only included in essay mode. Profile data (GPA, test scores) is sent on every request when the student has entered a GPA — this allows the LLM to contextualize statistics against the student's credentials in Q&A mode.

## Conversations

- Max 50, LRU eviction (oldest removed when creating new conversation at limit)
- Auto-titled from first user message (truncated at 60 chars)
- Mode-scoped: each conversation belongs to `'qa'` or `'essay'`
- Each conversation stores: selected `college`, `essayPrompt`, `messages[]`, timestamps
- Sidebar filters conversation list by current mode

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
├── hooks/
│   ├── useApi.ts              Health check + options fetch on mount
│   └── useStreaming.ts        SSE streaming hook (send/cancel)
└── components/
    ├── Sidebar.tsx            Mode selector + conversation list
    ├── ChatView.tsx           Message list + welcome state + streaming
    ├── MessageBubble.tsx      User/assistant message rendering
    ├── InputArea.tsx          Chat input + college picker + mode fields
    ├── CollegeCombobox.tsx    Searchable school dropdown (Headless UI)
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
