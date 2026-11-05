# Knowledge Base: Plan

Build a repeatable pipeline that collects feedback from a Discord channel and a set of Google Drive folders, then publishes one **Consolidated Feedback** Google Doc. Anyone on the team should be able to run it, each run should only read what's new, and every piece of feedback should be cited back to its source.

## Goals

- **One source of truth:** a single Google Doc, updated in place so its link never changes.
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
       Google Doc + Citations sheet (updated in place) ◄── synthesis ◄──────────────┘
```

The pipeline has two halves:

1. **Extraction (expensive, incremental).** New content is turned into structured feedback records by the LLM. Each record points to one or more citations. Records and citations are saved in state, so content is never extracted twice.
2. **Synthesis (cheap, full rebuild).** Every run regroups _all_ saved records by programming type and theme (see [Organizing feedback](#organizing-feedback-programming-types-then-themes)) and rewrites the doc. This avoids the duplicate and stale themes that come from appending. A **"New since last update"** section shows records first seen in this run.

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
- **What cursors miss:** messages edited or deleted after they were read, because neither creates a newer message ID. `kb update --full` reads the whole channel again, compares it to what's saved, extracts edited messages again, and marks deleted ones `removed`. Use it rarely, only when a full reload is actually needed (for example, after someone reports that important messages were edited or removed). Normal runs accept that later edits and deletions won't show up.

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
- **Citations sheet:** a Google Sheet in the Knowledge Base folder holds the complete running list, with one row per citation, including removed ones. It's rewritten on each run from `_state/citations.jsonl`.

## State (stored in Drive)

```
Knowledge Base/
├── Consolidated Feedback          # Google Doc, updated in place
├── Citations                      # Google Sheet, running list of all citations
├── raw/discord/YYYY-MM-DD.json    # new messages read in each run
├── raw/drive/<fileId>.md          # exported text of each source file
└── _state/
    ├── cursors.json               # Discord cursors + last run start time
    ├── manifest.json              # one entry per source item
    ├── citations.jsonl            # running list of all citations
    ├── extracted.jsonl            # saved feedback records
    ├── programming_types.json     # programming types found so far
    ├── run_log.json               # history of runs
    └── lock                       # prevents two runs at once
```

At the start of a run, the script takes the lock and downloads `_state/` into `data/state/`. At the end, it uploads `_state/` and releases the lock. If a lock is older than a set timeout, it's treated as stale and can be cleared with `--force-unlock`.

### Local copies

Everything fetched is also saved locally in `data/` (`data/raw/discord/`, `data/raw/drive/`, `data/state/`) for inspection and debugging. `data/` is in `.gitignore` and never committed. Drive is the source of truth for state, so the local copy is only a cache and can be deleted at any time.

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
  "feedback": "Not enough food vendors; stalls sold out early",
  "programming_type": "Night Market", // or null
  "category": "feature_request", // bug | feature_request | confusion | praise | other
  "sentiment": "negative",
  "citations": ["C-0042", "C-0045"],
  "first_seen": "2026-11-04T09:00:00Z"
}
```

## Output doc structure

1. Header: last updated, date range covered, number of sources and citations
2. **New since last update**
3. Executive summary (top takeaways across programming types and themes)
4. **Programming types**, ordered by amount of feedback. For each type:
   - A short summary, plus what's working and what to improve
   - Themes, each with a description, citation count, 2–3 quotes with authors, and the full list of citation markers
5. **Other themes** (feedback not tied to a programming type), ordered by amount of feedback, in the same format
6. Feature requests · Bugs (collected across everything, with citations)
7. Open questions / unclear feedback
8. Appendix: citations used in the doc (ID, author, date, context, link)

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
│   ├── extract.py       # item → feedback records + citations
│   ├── synthesize.py    # records → programming types/themes → markdown
│   └── publish.py       # update doc and Citations sheet, upload raw files
├── prompts/             # versioned extraction and synthesis prompts
├── tests/
├── scripts/smoke_test.py  # M0: check Discord + Drive access with real credentials
├── data/                # local copies of fetched data (gitignored)
├── secrets/             # service account key (gitignored)
├── config.example.yaml  # template for config.yaml (channel ID, folder IDs, file types, model)
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
kb status             # last run, cursors, item counts, recent errors, programming types with counts
kb rebuild            # extract everything again (e.g., after a big prompt change)
```

## Credentials and access

| What    | How                                                                                                                                                                                                                                                                                                                                                               | Who sets it up                    |
| ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| Discord | Bot token, Message Content Intent on, permissions View Channel + Read Message History                                                                                                                                                                                                                                                                             | Discord server admin (once)       |
| Google  | Service account with Drive, Docs, Sheets, and Slides APIs enabled. Each source folder is shared with it as **Viewer**, the Knowledge Base folder as **Editor**. The Knowledge Base folder should be in a **Shared Drive** (service account as Content manager), because service accounts have no Drive storage and can't create files in a normal My Drive folder. | Google Cloud project owner (once) |
| LLM     | Anthropic API key, with the model set in `config.yaml`                                                                                                                                                                                                                                                                                                            | Project owner                     |

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

- [ ] `sources/discord.py`: read the main channel and all threads (active and archived), remove bot messages, and keep each message's link
- [ ] Build items: one per thread, one per main-channel message (with the replied-to message as context)
- [ ] `sources/drive.py`: list the direct children of every source folder, filtered to Docs, Sheets, and Slides (subfolders are skipped)
- [ ] Exporters: Docs → markdown, Slides → slide text + speaker notes, Sheets → all tabs
- [ ] Write the output to the local `data/raw/` directory (gitignored)

**Done when:** one command produces local files for every thread, channel message, and in-scope Drive file; a person spot-checks 10 against the originals; and the speaker notes from at least one deck appear in its export.

### M2: State and incremental runs

- [ ] `state.py`: download and upload `_state/` to Drive, and the lock with stale-lock handling
- [ ] Discord cursors (main channel and per thread), saved only after a successful run
- [ ] Drive comparison: new / changed / skip / removed, based on the manifest and the last run's start time
- [ ] Content hashing, so changes that don't affect the text are skipped
- [ ] Recursive Drive listing: read subfolders of each source folder too
- [ ] `kb update --full` for edited and deleted Discord messages
- [ ] Upload raw snapshots to `raw/` in Drive
- [ ] `kb update --dry-run` and `kb status`
- [ ] Tests: new channel message, new message in an old thread, new thread, new Drive file, old file moved into the folder, edited file, comment-only change, removed file, and a crash before state is saved

**Done when:** a second run right after the first finds 0 new items; posting one Discord message, editing one Doc, and moving an old file into the folder makes exactly those three show up; and `--full` reports a deleted test message.

### M3: Extraction and citations

- [ ] `citations.py`: permanent IDs; one citation per Discord message and one per Drive file; active/removed status
- [ ] Extraction prompt (in `prompts/`, versioned) framed around community programming, with structured output: feedback, programming type or `null`, category, sentiment, and citations (Discord message IDs, or the file)
- [ ] Give the prompt the saved list of programming types, and add new types to `programming_types.json`
- [ ] Check every record: it must have at least one citation, and each cited Discord message must exist in the item
- [ ] New messages in an old thread: extract only those, with earlier messages as context
- [ ] Retries, plus per-item `error` status so one failure doesn't stop the run
- [ ] Token and cost summary at the end of each run

**Done when:** a full first run extracts records for every item; a person checks about 20 records to confirm the feedback is accurate, the programming type makes sense, and each citation link opens the right message or file; the programming types list has no obvious duplicates; and a second run makes 0 LLM calls.

### M4: Synthesis and publish

- [ ] `synthesize.py`: group records by programming type, then into themes within each type; group records with no type into LLM-found themes; merge duplicate type names; rank themes and pick quotes
- [ ] Give synthesis the previous run's type and theme names so the structure stays stable between runs
- [ ] Render the markdown doc in the structure above, with citation markers on every feedback point and the citations appendix
- [ ] `publish.py`: create the doc on the first run, then update it in place with `files.update`, and save the doc ID in state
- [ ] Write the Citations sheet from `citations.jsonl`

**Done when:** the doc exists in Drive and every feedback point has working citation links; the Citations sheet lists every citation; and after adding one new piece of feedback and re-running, it shows up under "New since last update" with a new citation ID while existing IDs and the doc link stay the same.

### M5: Hardening and handoff

- [ ] README: 5-minute setup, how to run, that `--full` is rarely needed and when to use it, what to do if something breaks
- [ ] Clear error messages for common problems (missing permission, expired token, lock held)
- [ ] Run log entries record who ran it, cursors before and after, and what changed
- [ ] A teammate does setup and a run using only the README

**Done when:** someone other than the author runs `kb update` successfully on their own machine.

---

## Future

**Scheduling.** For now runs are manual. Because state lives in Drive and Google access uses a service account, the same `kb update` can later run from GitHub Actions cron or a similar tool without redesign. That would mean moving secrets into the scheduler's secret store and adding failure notifications.

## Decisions

| Decision                  | Choice                                                                                                                                                                                           |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Author names in the doc   | Kept (Discord display name / Drive file owner)                                                                                                                                                   |
| Run frequency             | Manual (`kb update` on demand)                                                                                                                                                                   |
| Slides speaker notes      | Included (Slides API)                                                                                                                                                                            |
| How feedback is organized | By programming type (found by the LLM, no config), then themes; other feedback goes into LLM-found themes                                                                                        |
| Discord conversations     | One per thread; main-channel messages outside threads are separate items; no time-based grouping                                                                                                 |
| Discord incremental reads | Message-ID cursors per channel and thread. The first run reads everything automatically. `--full` is used rarely, only for a full reload; otherwise later edits and deletions are not picked up. |
| Drive incremental reads   | List all files each run; extract files not seen before or modified after the last run started; skip the rest                                                                                     |
| Citations                 | Discord: the exact message. Drive: the whole file. Permanent IDs; running list in `citations.jsonl` and the Citations sheet                                                                      |
