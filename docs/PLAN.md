# Knowledge Base: Plan

Build a repeatable pipeline that collects feedback from a Discord channel and a set of Google Drive folders, then publishes two Google Docs: **Consolidated Feedback**, and **Programs**, which describes each programming type and its history. Anyone on the team should be able to run it, each run should only read what's new, and every piece of feedback and every program fact should be cited back to its source.

## Goals

- **One source of truth:** two Google Docs (Consolidated Feedback and Programs), each updated in place so its link never changes.
- **Incremental:** each run reads only new or changed content.
- **Anyone can run it:** state lives in Drive, not on one person's machine, and setup takes about 5 minutes.
- **Cited:** every piece of feedback in the doc lists its citations. A running list of all citations is kept across runs.
- **Organized by programming type:** feedback about a type of programming (night market, campfire, dance, etc.) is grouped under it. Everything else is grouped into themes the LLM finds.
- **Manual runs:** someone runs `kb update` when they want a refresh. Nothing in the design prevents scheduling later (see [Future](#future)).

## Non-goals (for now)

- Scheduled or automatic runs.
- PDFs, images, and other file types besides Google Docs, Sheets, and Slides.
- Discord attachments (the links are saved, but files aren't downloaded or read).
- A search or chat interface over the knowledge base.

## Sources and scope

| Source                             | Approx. volume       | Included                                                         | Excluded                                    |
| ---------------------------------- | -------------------- | ---------------------------------------------------------------- | ------------------------------------------- |
| Discord channel (plus its threads) | hundreds of messages | human messages in the channel and its threads                    | bot messages, trivial replies ("+1", "lol") |
| Google Drive folders (listed in config; direct children only, recursive later) | ~1,000 files | Google Docs, Sheets (all tabs), Slides (including speaker notes) | PDFs, images, everything else               |

## Architecture

```
            ┌──────────────── every run ────────────────┐
Discord ─┐  │ Discord: read past message-ID cursors     │     new content       records +
         ├─►│ Drive: list files, keep new or modified   │──► LLM extraction ──► citations
Drive ───┘  │        since the last run                 │                          │
            └───────────────────────────────────────────┘                          │
  Feedback doc + Programs doc + Citations sheet ◄── synthesis ◄────────────────────┘
                 (updated in place)
```

The pipeline has two halves:

1. **Extraction (expensive, incremental).** The LLM turns new content into structured records. Each record has a `kind`: `feedback` (an opinion, complaint, request, or praise) or `program_info` (a fact describing a programming type or its history, such as what it is, when it ran, where, or how it changed). Each record points to one or more citations. Records and citations are saved in state, so content is never extracted twice.
2. **Synthesis (cheap, full rebuild).** Every run regroups _all_ saved records by programming type and theme (see [Organizing feedback](#organizing-feedback-programming-types-then-themes)) and rewrites both docs. This avoids the duplicate and stale themes that come from appending. A **"New since last update"** section shows records first seen in this run.

### Source items

An **item** is the unit sent to the LLM for extraction:

- **Discord thread:** all messages in one thread, including the message that started it. Threads are the only way Discord messages are grouped into conversations; there's no grouping by time.
- **Discord channel message:** a message posted in the main channel and not in a thread is its own item. If it's a reply, the message it replies to is included as context but isn't extracted again.
- **Drive file:** one Doc, Sheet, or Slides file. Long files are split into chunks for extraction, but they're still cited as the whole file.

## Incremental runs

### Discord: message-ID cursors

| Cursor                                         | How a run uses it                                                                                                                                                                                              |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Main channel: ID of the last message read      | Fetch only messages `after` that ID. Discord message IDs are time-ordered, so this works like a timestamp, with no ties.                                                                                       |
| Each thread: ID of the last message read in it | List active and archived threads. Skip any thread whose latest message ID isn't newer than its cursor. For the rest, fetch only messages after the cursor. A thread with no cursor is new and is read in full. |

- **New messages in an old thread:** only the new messages are extracted. The earlier messages are given to the LLM as read-only context so the new ones make sense.
- **First run:** there are no cursors yet, so the first `kb update` reads the whole channel and every thread. No flag is needed.
- **What cursors miss:** messages edited or deleted after they were read, because neither creates a newer message ID. `kb update --full` reads the whole channel again, compares it to what's saved, extracts edited messages again, and marks deleted ones `removed`. `--full` also exports every Drive file again, whether or not it was modified, so a change to how files are exported (such as removing images) reaches every file. Content hashes stop this from causing re-extraction when the text is the same. Use it rarely, only when a full reload is actually needed (for example, after someone reports that important messages were edited or removed). Normal runs accept that later edits and deletions won't show up.

### Drive: compare the file list to the manifest

Every run lists all in-scope files in the source folders (metadata only: ID, name, type, `modifiedTime`). Listing ~1,000 files is fast. Each file is then sorted into one of these groups:

| File                                                            | Action                                                                                                             |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| **Not in the manifest** (never seen)                            | New: export and extract. This includes files that were moved into a source folder, even if their `modifiedTime` is old. |
| In the manifest, `modifiedTime` **after** the last run started  | Changed: export, extract again, and replace its old records                                                        |
| In the manifest, `modifiedTime` **before** the last run started | Skip                                                                                                               |
| In the manifest but **not in the listing**                      | Removed: mark its citation `removed` and drop its records from the doc                                             |

- **Compare against when the last run started, not when it finished.** A file edited while a run was in progress is then picked up next time.
- **A content hash skips changes that don't affect the text.** For example, adding a comment updates `modifiedTime` but not the exported text, so the file isn't extracted again.

### Applies to both

- **Cursors and the last-run time are saved only after a run finishes and publishes.** If a run crashes, the next one starts from the same place. Records already extracted are not extracted again, because they're keyed by message ID, or by file ID plus content hash.

## Organizing feedback: programming types, then themes

A **programming type** is a kind of activity the community runs, such as a night market, campfire, or dance. Feedback like "the night market needs more food stalls" belongs to _Night Market_. Feedback like "the booking app keeps logging me out" isn't about any programming type, so it goes into the general themes.

No config file lists the programming types. The LLM finds them, with this framing:

- **Extraction:** the prompt explains that feedback is mostly about community programming. For each record, the LLM names the programming type it's about, or `null` if it isn't about any type. To keep names consistent, it's given the list of programming types found so far (saved in `_state/programming_types.json`). It reuses an existing name when one fits, and creates a new one only when nothing fits. For example, "Night Market" is reused rather than adding "Evening Market."
- **Synthesis:** records are grouped by programming type, and into themes within each type. Records with no programming type are grouped into themes the LLM finds. The previous run's type and theme names are given as a starting point so the doc's structure stays stable between runs.
- **Program info:** `program_info` records use the same programming type names. They're never `null`, since a fact that isn't about a programming type isn't kept. Every fact is dated with the period it applies to (`applies_to`), descriptions included, because a program's setup can change from year to year.
- **How a program's history is built:** a change shows up in one of two ways:
  - **A source states it**, for example a 2024 recap saying "this year we split Night Market into three weekly nights." That becomes one `history` record.
  - **Sources from different periods describe different setups**, for example a 2023 plan saying "one night, courtyard" and a 2024 plan saying "three nights, north lot," and no source mentions a change. Extraction sees one item at a time, so it can't catch this. Instead, each `description` fact is treated as a dated snapshot. Synthesis compares the snapshots for each programming type and writes the change into the history, citing the sources on both sides of it.

  The current description comes from the most recent snapshot. When sources about the same period disagree, the timeline notes the disagreement and cites both sources, rather than choosing one.
- **Dating feedback:** feedback records are dated the same way, with `applies_to` meaning the period the feedback is about, not when it was given. For example, a 2025 message saying "the 2023 Night Market ran out of food" is about 2023. The date it was given stays on the citation. Because feedback and program facts are dated the same way, synthesis can:
  - show which years each theme's feedback comes from
  - match feedback to the program setup it was about (for example, "sold out early" complaints in 2023, under the one-night setup, and none in 2024, under the three-night setup)
  - label a theme **likely resolved** when its feedback stops after a program change, rather than listing it as a current problem
  - rank themes and pick executive-summary takeaways with recent feedback weighted above old feedback, so a theme with many complaints from 2022 doesn't outrank this year's problems
- **Cleanup:** if two names turn out to mean the same thing, synthesis can merge them and update the saved list. `kb status` shows each programming type and how many records it has, so odd ones are easy to spot.

## Citations

Every piece of feedback in the doc lists the citations it came from. The full list of citations is kept and grows with each run.

| Source                      | What a citation points to | Link                                                               |
| --------------------------- | ------------------------- | ------------------------------------------------------------------ |
| Discord                     | one message               | message link (`discord.com/channels/<server>/<channel>/<message>`) |
| Google Doc / Sheet / Slides | the whole file            | the file's Drive link                                              |

How the running list works:

- Each citation gets a permanent ID (`C-0001`, `C-0002`, …) when it's first seen. IDs are never reused or renumbered. The run lock keeps numbering safe when several people run the pipeline.
- Each Drive file has exactly one citation. When the file is edited, it keeps the same citation ID.
- One feedback record can have several citations, for example several messages in a thread saying the same thing. When synthesis groups records into a theme, the theme lists every citation from its records.
- Deleted sources keep their citation in the list, marked `removed`. Feedback whose citations have all been removed is dropped from the doc.

Where citations appear:

- **In the doc:** each feedback point ends with linked markers such as `[C-0042] [C-0107]`, and each theme shows its citation count. An appendix lists every citation used in the doc.
- **Citations sheet:** a Google Sheet in the Knowledge Base folder holds the complete running list, with one row per citation, including removed ones. It's rewritten on each run from `_state/citations.jsonl`. A second tab, **Records**, has one row per record (ID, kind, programming type, `applies_to`, `date_basis`, category, sentiment, the feedback or fact, and citation IDs), so feedback can be filtered by year, type, or category, which the docs can't do. It's rewritten from `_state/extracted.jsonl`.

## State (stored in Drive)

```
Knowledge Base/
├── Consolidated Feedback          # Google Doc, updated in place
├── Programs                       # Google Doc, updated in place
├── Citations                      # Google Sheet, running list of all citations
├── raw/                           # copies of fetched content (see below)
│   ├── discord/YYYY-MM-DD.json    # new messages read in each run that day
│   └── drive/<fileId>.md          # latest export of each Drive file
└── _state/
    ├── cursors.json               # Discord cursors + last run start time
    ├── manifest.json              # one entry per source item
    ├── citations.jsonl            # running list of all citations
    ├── extracted.jsonl            # saved records (feedback and program_info)
    ├── programming_types.json     # programming types found so far
    ├── run_log.json               # history of runs
    └── lock                       # prevents two runs at once
```

At the start of a run, the script takes the lock and downloads `_state/` into `data/state/`. At the end, it uploads `_state/` and releases the lock. If a lock is older than a set timeout, it's treated as stale and can be cleared with `--force-unlock`.

### Local copies

Everything fetched is saved locally in `data/` (`data/raw/discord/YYYY-MM-DD.json` for new messages read in each run, `data/raw/drive/<fileId>.md` for exported text, and `data/state/`) for inspection and debugging. Each `kb update` also uploads the run's new Discord messages and new or changed Drive exports to `raw/` in the Knowledge Base folder, so anyone on the team can see exactly what the model was given without pulling everything again. With embedded images removed, all Drive exports total about 2.8 MB, so storage isn't a concern. `kb pull` stays local only. `data/raw/discord/members.json` caches the server nickname of every Discord author already looked up (user ID → username, nickname, display name), so each run only looks up new authors; it's local only. `data/` is in `.gitignore` and never committed. Drive is the source of truth for state, so the local copy is only a cache and can be deleted at any time.

### Cursors

```json
{
  "discord_channel": "1301234567890123456", // last message ID read in the main channel
  "discord_threads": {
    "1299876543210987654": "1302222222222222222" // thread ID → last message ID read
  },
  "last_run_started_at": "2026-11-04T09:00:00Z" // Drive files modified after this are re-checked
}
```

### Manifest entry

```json
{
  "id": "drive:1AbC...", // or "discord-thread:<id>" / "discord-msg:<id>"
  "source": "drive",
  "title": "Night market debrief – Oct",
  "link": "https://docs.google.com/...",
  "modified_time": "2026-10-30T14:02:11Z",
  "content_hash": "sha256:...",
  "prompt_version": 1,
  "first_seen": "2026-11-04T09:00:00Z",
  "last_processed": "2026-11-04T09:00:00Z",
  "status": "ok" // ok | error | removed | skipped
}
```

### Citation

```json
{
  "id": "C-0042",
  "item_id": "discord-thread:1299876543210987654",
  "source": "discord", // discord | doc | sheet | slides
  "link": "https://discord.com/channels/.../1302111111111111111", // message link, or Drive file link
  "author": "Discord display name or Drive file owner",
  "date": "2026-10-16T19:22:05Z", // message time, or file modifiedTime
  "context": "#feedback › thread: Night market thoughts", // or file name
  "excerpt": "Ran out of food by 8pm",
  "first_seen": "2026-11-04T09:00:00Z",
  "status": "active" // active | removed
}
```

### Extracted record

```json
{
  "record_id": "R-0187",
  "item_id": "discord-thread:1299876543210987654",
  "kind": "feedback",
  "feedback": "Not enough food vendors; stalls sold out early",
  "programming_type": "Night Market", // or null
  "category": "feature_request", // bug | feature_request | confusion | praise | other
  "sentiment": "negative",
  "applies_to": "2023-10", // the period the feedback is about (see below)
  "date_basis": "inferred", // stated | inferred | source_date
  "citations": ["C-0042", "C-0045"],
  "first_seen": "2026-11-04T09:00:00Z"
}
```

A `program_info` record has the same IDs, `kind`, `programming_type` (required, never `null`), `citations`, and `first_seen`, but no `feedback`, `category`, or `sentiment`. A `description` fact describes the setup during the period in `applies_to`. A `history` fact describes a change or a one-time event:

```json
{
  "record_id": "R-0188",
  "item_id": "drive:1AbCdEfGhIjKlMnOp",
  "kind": "program_info",
  "fact": "Night Market moved from the courtyard to the north parking lot to fit more vendors",
  "info_type": "history", // description | history
  "applies_to": "2025-09", // the period the fact is true for, as precise as the source allows (YYYY, YYYY-MM, or YYYY-MM-DD)
  "date_basis": "stated", // stated | inferred | source_date
  "programming_type": "Night Market",
  "citations": ["C-0311"],
  "first_seen": "2026-11-04T09:00:00Z"
}
```

Every record, of either kind, has `applies_to` and `date_basis`. For feedback, `applies_to` is the period the feedback is about. For a `program_info` fact, it's the period the fact is true for. `date_basis` records how `applies_to` was worked out:

- `stated`: the source gives the date ("in September 2025 we moved…").
- `inferred`: the model worked it out from clues such as a file name ("Night Market 2023 Plan") or "this year" in a message whose date is known.
- `source_date`: there were no clues, so the citation's date was used (the message time, or the file's `modifiedTime`). A file's `modifiedTime` is unreliable, because a 2023 doc edited in 2025 looks like 2025. Synthesis gives these dates less weight and marks them as approximate in the timeline.

`applies_to` is never `null`, because every record can fall back to a `source_date`. Most Discord feedback will be `source_date`, which is usually right, since people mostly react to something recent. If a record has several citations, its `source_date` is the earliest citation's date.

## Output doc structure

### Consolidated Feedback

1. Header: links to the Programs doc and the Citations sheet, last updated, date range covered (the earliest to latest `applies_to`), number of sources and citations
2. **New since last update**
3. Executive summary (top takeaways across programming types and themes, weighted toward recent feedback)
4. **Programming types**, ordered by amount of feedback, with recent feedback weighted higher. For each type:
   - A short summary, plus what's working and what to improve
   - Themes, each with a description, citation count, a breakdown by year (for example, "2023: 12 · 2024: 3"), the most recent date it was mentioned, 2–3 quotes with authors (recent ones preferred), and the full list of citation markers. A theme whose feedback stopped after a program change is labeled **likely resolved**, with the change it followed, and listed after current themes
5. **Other themes** (feedback not tied to a programming type), ordered the same way, in the same format
6. Feature requests · Bugs (collected across everything, with citations, the most recent date each was mentioned, and **likely resolved** labels)
7. Open questions / unclear feedback
8. Appendix: citations used in the doc (ID, author, date, context, link)

### Programs

Built from `program_info` records, plus a feedback summary for each period from the same synthesis run.

1. Header: last updated, number of programming types, link to Consolidated Feedback
2. Index of programming types, alphabetical, linking to each section
3. For each programming type:
   - **Description:** what it is, who it's for, and how it runs now (from the most recent snapshot), with citation markers
   - **History:** a timeline ordered by `applies_to`, with changes stated by a source and changes found by comparing snapshots, conflicting sources noted, and approximate (`source_date`) dates marked; each entry has citation markers, and a change found by comparison cites the sources on both sides
   - **Feedback by period:** for each setup in the history, two or three sentences on the feedback about that period (for example, what people said about the one-night setup in 2023 compared with the three-night setup in 2024), and a link to that type's section in Consolidated Feedback
4. Appendix: citations used in the doc

## Choosing models

Extraction and synthesis use separate models, set in `config.yaml`, because their mistakes have different costs:

| | Extraction | Synthesis |
| --- | --- | --- |
| Volume | Every item, once | All saved records, every run |
| Mistakes | Permanent: saved records are never extracted again | Temporary: fixed on the next run |
| Changing the model later | Means paying to extract everything again | A config change; the next run rebuilds both docs |
| Hard parts | Telling feedback apart from program facts, inferring `applies_to`, reusing programming type names | Grouping hundreds of records, comparing snapshots to find changes, "likely resolved" labels, merging duplicate type names |
| Rough cost (Opus 5.5) | About $5–8 for the first full run with the Batch API, then small amounts | About $3–5 per run (about $1.5–2.5 on Sonnet 5.5) |

- **Extraction: Claude Opus 5.5**, through the Batch API (half price) for the first run and any large run. Quality matters most here because mistakes are permanent. During the M3 spot check, run the same items through Sonnet 5.5 as well, and switch if it's just as accurate.
- **Synthesis: Claude Opus 5.5 by default.** A cheaper model is a reasonable choice here, because nothing is locked in: if the docs come out worse, switch back and run again, with no extraction cost. During M4, render both docs with Opus 5.5 and Sonnet 5.5 once and keep the cheaper one if the difference doesn't matter.
- **Not Haiku** for either step: extraction mistakes are permanent, and synthesis reasons over many records at once.

## Repository layout

```
lyf-knowledge-base/
├── kb/
│   ├── cli.py           # update / rebuild / status
│   ├── config.py        # loads config.yaml + .env
│   ├── state.py         # Drive sync for _state/, lock, cursors, manifest
│   ├── citations.py     # citation registry: ID assignment, links, status
│   ├── sources/
│   │   ├── discord.py   # channel + threads, read after cursors
│   │   └── drive.py     # list files, compare to manifest, export Docs/Sheets/Slides
│   ├── pull.py          # M1: full read of both sources into data/raw/
│   ├── update.py        # one `kb update` run: lock, read what's new, save raw + state
│   ├── extract.py       # item → feedback records + citations
│   ├── synthesize.py    # records → programming types/themes → markdown
│   └── publish.py       # update doc and Citations sheet
├── prompts/             # versioned extraction and synthesis prompts
├── tests/
├── scripts/smoke_test.py  # M0: check Discord + Drive access with real credentials
├── data/                # local copies of fetched data (gitignored)
├── secrets/             # service account key (gitignored)
├── config.example.yaml  # template for config.yaml (channel ID, folder IDs, file types, models)
├── config.yaml          # local copy with real IDs (gitignored)
├── .env.example
├── .gitignore
├── pyproject.toml       # dependencies; installs the `kb` command
└── README.md            # setup: Discord bot, service account, Drive access
```

### CLI

```bash
kb update             # read new Discord messages + new/changed Drive files → extract → rebuild doc
kb update --dry-run   # show what's new or changed; no LLM calls, no writes
kb update --full      # rare: read the whole Discord channel again to catch edited or deleted messages
kb status             # last run, cursors, item counts, recent errors, programming types with counts, records per year, share of source_date dates
kb rebuild            # extract everything again (e.g., after a big prompt change)
kb pull               # M1/debugging: full read of Discord + Drive into data/raw/ (local only)
```

## Credentials and access

| What    | How                                                                                                                                                                                                                                                                                                                                                               | Who sets it up                    |
| ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| Discord | Bot token, Message Content Intent on, permissions View Channel + Read Message History                                                                                                                                                                                                                                                                             | Discord server admin (once)       |
| Google  | Service account with Drive, Docs, Sheets, and Slides APIs enabled. Each source folder is shared with it as **Viewer**, the Knowledge Base folder as **Editor**. The Knowledge Base folder should be in a **Shared Drive** (service account as Content manager), because service accounts have no Drive storage and can't create files in a normal My Drive folder. | Google Cloud project owner (once) |
| LLM     | Anthropic API key, with the models set in `config.yaml`                                                                                                                                                                                                                                                                                                            | Project owner                     |

Secrets go in `.env` and the key file in `secrets/` (both gitignored). Each person who runs the pipeline needs their own `.env` with the shared credentials. Step-by-step setup is in the [README](../README.md).

---

## Milestones

Each milestone ends with something you can demo, and doesn't depend on later milestones.

### M0: Access and skeleton

- [x] Create the Discord bot, enable Message Content Intent, and invite it to the server
- [x] Create the Google Cloud project, enable the Drive, Docs, Sheets, and Slides APIs, and create the service account
- [x] Share the source folder (Viewer) with the service account
- [x] Create the Knowledge Base folder in a Shared Drive and add the service account as Content manager
- [x] README with setup instructions for the bot, service account, and Drive access
- [x] `.gitignore` covering `.env`, `config.yaml`, `secrets/`, and `data/`
- [x] Repo skeleton: `kb/` package, `config.example.yaml`, `.env.example`, dependency file
- [x] `git init` and the first commit
- [x] Smoke-test script (`scripts/smoke_test.py`)

**Done when:** a smoke-test script can read one channel message and one thread message, and list one Drive file, using the real credentials.

### M1: Raw pulls (local only, full read)

- [x] `sources/discord.py`: read the main channel and all threads (active and archived), remove bot messages, and keep each message's link
- [x] Build items: one per thread, one per main-channel message (with the replied-to message as context)
- [x] `sources/drive.py`: list the direct children of every source folder, filtered to Docs, Sheets, and Slides (subfolders are skipped)
- [x] Exporters: Docs → markdown, Slides → slide text + speaker notes, Sheets → all tabs
- [x] Write the output to the local `data/raw/` directory (gitignored)

**Done when:** one command produces local files for every thread, channel message, and in-scope Drive file; a person spot-checks 10 against the originals; and the speaker notes from at least one deck appear in its export.

### M2: State and incremental runs

- [x] `state.py`: download and upload `_state/` to Drive, and the lock with stale-lock handling
- [x] Discord cursors (main channel and per thread), saved only after a successful run
- [x] Drive comparison: new / changed / skip / removed, based on the manifest and the last run's start time
- [x] Content hashing, so changes that don't affect the text are skipped
- [x] Recursive Drive listing: read subfolders of each source folder too
- [x] `kb update --full` for edited and deleted Discord messages
- [x] Save raw snapshots locally in `data/raw/`
- [x] `kb update --dry-run` and `kb status`
- [x] Tests: new channel message, new message in an old thread, new thread, new Drive file, old file moved into the folder, edited file, comment-only change, removed file, and a crash before state is saved
- [x] Local cache of Discord server nicknames already looked up (`data/raw/discord/members.json`), so only new authors are looked up; `--full` and `kb pull --refresh-names` look everyone up again

**Done when:** a second run right after the first finds 0 new items; posting one Discord message, editing one Doc, and moving an old file into the folder makes exactly those three show up; and `--full` reports a deleted test message.

### M3: Extraction and citations

Before the first full run, cut the text sent to the LLM down to what matters. A check of the first 277 exported Drive files found 29.8 MB of text, but 27 MB of it was images embedded as base64 inside Docs exports (55 files). The largest file, "2026 Universal", was 7.6 MB, and 53 KB once images were removed. Without images the files total 2.8 MB (about 0.7M tokens), with a median of 5.7 KB and a largest file of 161 KB.

- [ ] Docs exporter: remove embedded base64 images from Drive's markdown export (the `[imageN]: <data:...>` definitions), and replace each `![][imageN]` with `[image]` so the model knows an image was there. This happens in the exporter, before the text is saved or hashed, so replacing a picture in a Doc doesn't change its content hash or cause it to be extracted again. The images stay in the original Doc, which every citation links to
- [ ] `kb update --full` exports every Drive file again, so the image removal reaches files already in the manifest. Run it once after the exporter change
- [ ] Upload raw snapshots to `raw/` in the Knowledge Base folder again (new Discord messages per day, new or changed Drive exports), now that they're small
- [ ] Separate models for the two steps in `config.yaml`: `llm.extract_model` (default `claude-opus-5-5`, run through the Batch API for large runs) and `llm.synthesis_model` (default `claude-opus-5-5`). See [Choosing models](#choosing-models)
- [ ] Size check: estimate tokens for each item (characters ÷ 4 is close enough) and show the per-item estimate, the total, and the estimated cost in `kb update --dry-run`. Items over `llm.max_item_tokens` in `config.yaml` (default 100,000) are given status `skipped` with the reason, and listed in the run summary and `kb status`, unless their file ID is in `drive.allow_large_files`
- [ ] `citations.py`: permanent IDs; one citation per Discord message and one per Drive file; active/removed status
- [ ] Extraction prompt (in `prompts/`, versioned) framed around community programming, with structured output. Each record has a `kind`:
  - `feedback`: feedback, programming type or `null`, category, sentiment, `applies_to` and `date_basis` (the period the feedback is about, not when it was given), and citations (Discord message IDs, or the file)
  - `program_info`: a description or history fact, `info_type`, `applies_to` and `date_basis`, programming type (required), and citations. The prompt asks for the period each fact is true for, using what the source says or clues such as the file name and message date, and to use `source_date` only when there are no clues
- [ ] Give the prompt the saved list of programming types, and add new types to `programming_types.json`
- [ ] Check every record: it must have at least one citation, each cited Discord message must exist in the item, `program_info` records must name a programming type, and every record must have an `applies_to` in `YYYY`, `YYYY-MM`, or `YYYY-MM-DD` form. If `date_basis` is `source_date`, `applies_to` is set from the citation's date in code, not taken from the model
- [ ] New messages in an old thread: extract only those, with earlier messages as context
- [ ] Retries, plus per-item `error` status so one failure doesn't stop the run
- [ ] Token and cost summary at the end of each run
- [ ] `kb status`: records per year, and the share with `date_basis: source_date`, so weak dating is easy to spot

**Done when:** no exported Doc contains base64 image data; `--dry-run` shows token and cost estimates, and a file over the size limit is skipped and reported unless it's allowed in config; a full first run extracts records for every item that isn't skipped; a person checks about 20 records to confirm the feedback and program facts are accurate, the programming type makes sense, records have the right `applies_to` (including feedback about a past year), and each citation link opens the right message or file; the programming types list has no obvious duplicates; and a second run makes 0 LLM calls.

### M4: Synthesis and publish

- [ ] `synthesize.py`: group `feedback` records by programming type, then into themes within each type; group records with no type into LLM-found themes; merge duplicate type names; rank themes with recent feedback weighted higher, and pick quotes (recent ones preferred). For each theme, add its breakdown by year and the most recent date it was mentioned
- [ ] Likely-resolved check: for each theme tied to a programming type, compare when its feedback stops with the program's history, and label it **likely resolved** if it stopped after a change. Apply the same check to feature requests and bugs
- [ ] Give synthesis the previous run's type and theme names so the structure stays stable between runs
- [ ] Render the markdown doc in the structure above, with citation markers on every feedback point and the citations appendix
- [ ] Programs synthesis: for each programming type, sort its `program_info` records by `applies_to`, treat `description` facts as dated snapshots, and compare snapshots from different periods to find changes no source states outright. Write the current description from the latest snapshot, a history timeline that merges stated and found changes (each found change cites both sides), notes on conflicting sources, and a feedback summary for each period of the history
- [ ] Render the Programs doc in the structure above, with citation markers and links to and from Consolidated Feedback
- [ ] `publish.py`: create each doc on the first run, then update it in place with `files.update`, and save both doc IDs in state
- [ ] Write the Citations sheet from `citations.jsonl`, and its Records tab from `extracted.jsonl`

**Done when:** both docs exist in Drive and every feedback point and program fact has working citation links; each programming type in Programs has a description and a history in date order; a test programming type whose 2023 and 2024 docs describe different setups, with no source mentioning the change, shows that change in its history with citations to both docs; a theme whose feedback stops after a program change is labeled **likely resolved**; the Records tab can be filtered to one year; the Citations sheet lists every citation; and after adding one new piece of feedback and re-running, it shows up under "New since last update" with a new citation ID while existing IDs and both doc links stay the same.

### M5: Hardening and handoff

- [ ] README: 5-minute setup, how to run, that `--full` is rarely needed and when to use it, what to do if something breaks
- [ ] Clear error messages for common problems (missing permission, expired token, lock held)
- [ ] Run log entries record who ran it, cursors before and after, and what changed
- [ ] A teammate does setup and a run using only the README

**Done when:** someone other than the author runs `kb update` successfully on their own machine.

---

## Future

**Scheduling.** For now runs are manual. Because state lives in Drive and Google access uses a service account, the same `kb update` can later run from GitHub Actions cron or a similar tool without redesign. That would mean moving secrets into the scheduler's secret store and adding failure notifications.

**Reports for a single year.** Because every record is dated, a later command could publish a report for one year, such as a 2025 review, built from the same records with no new extraction.

## Decisions

| Decision                  | Choice                                                                                                                                                                                           |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Author names in the doc   | Kept (Discord display name / Drive file owner)                                                                                                                                                   |
| Run frequency             | Manual (`kb update` on demand)                                                                                                                                                                   |
| Slides speaker notes      | Included (Slides API)                                                                                                                                                                            |
| How feedback is organized | By programming type (found by the LLM, no config), then themes; other feedback goes into LLM-found themes                                                                                        |
| Program descriptions and history | Extracted as `program_info` records alongside feedback, in the same pass; published as a separate **Programs** doc (not a tab, because the markdown upload in `files.update` can't target Docs tabs). Every fact is dated (`applies_to`, `date_basis`); synthesis finds changes by comparing dated snapshots |
| Dating feedback | Every feedback record has `applies_to` (the period it's about, not when it was given) and `date_basis`, like program facts. Used for year breakdowns, matching feedback to program setups, **likely resolved** labels, and weighting recent feedback higher. The Citations sheet's Records tab allows filtering by year |
| Discord conversations     | One per thread; main-channel messages outside threads are separate items; no time-based grouping                                                                                                 |
| Discord incremental reads | Message-ID cursors per channel and thread. The first run reads everything automatically. `--full` is used rarely, only for a full reload; otherwise later edits and deletions are not picked up. |
| Drive incremental reads   | List all files each run; extract files not seen before or modified after the last run started; skip the rest                                                                                     |
| Models                    | `llm.extract_model` and `llm.synthesis_model`, both Claude Opus 5.5 by default; synthesis can be switched to a cheaper model at any time (see [Choosing models](#choosing-models)) |
| Raw snapshots             | Saved locally in `data/raw/` and uploaded to `raw/` in the Knowledge Base folder by `kb update` (small once images are removed); `kb pull` stays local |
| Large files and images    | Embedded images are removed from Docs exports. Items over `llm.max_item_tokens` (default 100,000) are skipped and reported unless allowed in config, so one huge file can't dominate the cost |
| Citations                 | Discord: the exact message. Drive: the whole file. Permanent IDs; running list in `citations.jsonl` and the Citations sheet                                                                      |
