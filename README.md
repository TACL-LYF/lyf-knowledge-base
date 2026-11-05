# LYF Knowledge Base

Collects feedback from a Discord channel and a set of Google Drive folders and publishes three things in the shared **Knowledge Base** Drive folder:

- **Consolidated Feedback** (Google Doc): feedback grouped by programming type (night market, campfire, workshops, …) and then by theme, with what's new since the last update and an executive summary.
- **Programs** (Google Doc): what each programming type is, how it changed over the years, how to run it, and what people said about each version.
- **Citations** (Google Sheet): every source cited, and every extracted record, filterable by year, type, or category.

Every piece of feedback and every program fact links back to the Discord message or Drive file it came from. Each run reads only what's new since the last run, and the docs are updated in place, so their links never change.

> **Status:** M5 (hardening and handoff). The pipeline runs end to end; what's left is a teammate doing setup and a run from this README alone. The design and milestones are in [docs/PLAN.md](docs/PLAN.md).

## Contents

- [Quick start (about 5 minutes)](#quick-start-about-5-minutes)
- [Running it](#running-it)
- [Commands](#commands)
- [When to use the rare options](#when-to-use-the-rare-options)
- [If something goes wrong](#if-something-goes-wrong)
- [How it works](#how-it-works)
- [Local data](#local-data)
- [One-time project setup](#one-time-project-setup) (Discord bot, Google service account, Drive folders)
- [Development](#development)

---

## Quick start (about 5 minutes)

This is for **anyone who runs the pipeline**. It assumes the one-time project setup is already done (it is for LYF). You don't need any Discord or Drive permissions of your own: the pipeline uses a bot and a service account.

**You need:** Python 3.11 or newer, `git`, and access to the team's password manager.

1. **Get the code:**

   ```bash
   git clone https://github.com/TACL-LYF/lyf-knowledge-base.git
   cd lyf-knowledge-base
   python3 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -e '.[dev]'
   ```

2. **Get three credentials** from the team's password manager:
   - the Discord bot token
   - the service account key file: save it as `secrets/kb-pipeline.json` (create the `secrets/` folder)
   - the Anthropic API key

3. **Fill in `.env`** (secrets):

   ```bash
   cp .env.example .env
   ```

   ```bash
   DISCORD_BOT_TOKEN=...
   GOOGLE_APPLICATION_CREDENTIALS=secrets/kb-pipeline.json
   ANTHROPIC_API_KEY=...
   ```

4. **Fill in `config.yaml`** (IDs, not secret). Copy the template and paste in the Discord server and channel IDs and the Drive folder IDs. They're kept in the password manager next to the credentials; if they aren't there, `config.yaml` from anyone who already runs it works as is:

   ```bash
   cp config.example.yaml config.yaml
   ```

   ```yaml
   discord:
     server_id: "..."
     channel_id: "..."
   drive:
     source_folder_ids:
       - "..."
       - "..."
     kb_folder_id: "..."
   ```

   Leave the other settings at their defaults.

5. **Check access:**

   ```bash
   python scripts/smoke_test.py
   ```

   It reads one Discord message and one thread message, lists one file in each source folder, checks that the Knowledge Base folder is writable, and checks the Anthropic key and models. Nothing is written anywhere, and no tokens are used. Every line should say `PASS`; a `FAIL` line says what to fix (see also [If something goes wrong](#if-something-goes-wrong)).

6. **See where things stand:**

   ```bash
   kb status
   ```

   This shows the last run and who did it, item counts, any errors, and the links to the published docs.

That's it. `.env`, `config.yaml`, `secrets/`, and `data/` are in `.gitignore`; never commit them.

---

## Running it

```bash
kb update --dry-run   # optional: what's new, and the estimated cost; nothing is sent or saved
kb update             # read what's new, extract it, and update the docs
```

A normal run:

1. Takes a lock in Drive, so two people can't run at once.
2. Downloads the shared state (`_state/` in the Knowledge Base folder).
3. Reads new Discord messages and new or changed Drive files.
4. Sends each new item to Claude, which pulls out cited **records** (feedback and program facts).
5. Rebuilds and republishes the two docs and the Citations sheet, but only the sections whose records changed. If nothing changed, this step is skipped and no LLM calls are made.
6. Uploads the state and releases the lock.

Each stage prints its progress and result, and the run ends with a summary: what was new, what was extracted, tokens and cost, and the doc links.

**How long and how much.** A run with a handful of new items usually takes a few minutes and costs cents. Big runs (20 or more requests in one step) go through the Batch API at half price, which can take up to about an hour; leave it running. `kb update --dry-run` always shows the estimate first. For reference, extracting everything from scratch costs about $5–8 and rebuilding both docs from scratch about $4, with Claude Opus 5.5.

**Anyone can run it, any time.** All state lives in Drive, so it doesn't matter who ran it last or from which computer. `kb log` shows recent runs: who ran them, how the cursors moved, and which items changed.

**Ctrl-C is safe.** Requests already sent are finished, nothing more is sent, and the lock is released. Nothing in `_state/` changes unless a run completes, so the next run starts from the same place. Answers already received are cached locally, so they aren't paid for twice if you run again on the same machine.

---

## Commands

| Command | What it does |
| --- | --- |
| `kb update` | The normal run: read what's new, extract, update the docs. |
| `kb update --dry-run` | Show what's new and the estimated cost. No LLM calls, nothing saved, no lock. |
| `kb status` | Last run, lock, cursors, item counts, errors, skipped files, record counts, doc links. |
| `kb log` | Recent runs, newest first (`-n 20` for more, `--items 50` to list more items per run). |
| `kb update --limit N` | Extract at most N items; the rest wait as `pending` for the next run. |
| `kb update --source discord` | Read and extract only Discord (or `drive`). Combines with the flags here. |
| `kb update --pull` | Only read Discord and Drive; new items wait as `pending`. Doesn't need the Anthropic key. |
| `kb update --extract` | Only extract `pending` items, from the copies the last pull saved. Doesn't read Discord or Drive. |
| `kb update --synthesize` | Only rebuild and publish the docs from saved records, even if nothing changed. |
| `kb update --force-unlock` | Clear a lock left by an interrupted run (see [the lock](#another-run-is-in-progress)). |
| `kb update --full` | Rare: read the whole Discord channel and export every Drive file again. |
| `kb requeue` | Rare: mark every item to be extracted again, without doing it yet. |
| `kb rebuild` | Rare: extract every item again now. Costs about as much as the first run; asks first. |
| `kb pull` | Debugging: full read of Discord and Drive into `data/raw/` only. Writes nothing to Drive. |

Every command has `--help`.

---

## When to use the rare options

### `--full`: almost never

A normal run reads only Discord messages **after** the last one it saw, so it never notices a message that was **edited or deleted** after it was read. Drive is different: every run lists every file and picks up edits, new files, moved files, and removed files on its own.

Use `kb update --full` only when:

- **Someone edited or deleted Discord messages** and you want the docs to reflect it (for example, a person asks for their feedback to be removed: delete the message, then run `--full`). Edited messages are extracted again; deleted ones have their citations marked removed and drop out of the docs.
- **The way files are exported changed** (a code change to the Drive exporters), so every file needs exporting again. Files whose text comes out the same are still skipped, so they aren't extracted again.

It re-reads the whole channel and every file, so it takes longer (a few minutes) but doesn't cost much more in LLM calls, because unchanged items aren't extracted again.

### `kb rebuild` and `kb requeue`: after a big prompt change

Extraction results are saved for good: once an item is extracted, it's never sent to the model again unless it changes. If the extraction prompt (`prompts/extract_v*.md`) or `llm.extract_model` changes enough that old records should be redone:

- `kb rebuild` does it all in one go (costs about as much as the first run; `kb rebuild --dry-run` shows the estimate).
- `kb requeue` only marks everything to extract again, so you can check a sample before paying for the rest:

  ```bash
  kb requeue
  kb update --pull --source discord          # re-read Discord messages (only hashes are kept after extraction)
  kb update --extract --dry-run --limit 10   # cost of a 10-item sample
  kb update --extract --limit 10             # the sample
  kb update --extract                        # the rest
  ```

Citation IDs stay the same either way, and each item keeps its old records until its new ones replace them (`prompt_version` on each record says which prompt made it).

Changing the **synthesis** model or prompts doesn't need any of this: run `kb update --synthesize`, which rebuilds the docs from the saved records with no extraction cost.

### A small first run

On a brand-new Knowledge Base folder (no `_state/` yet), the first `kb update` reads and extracts everything. Check that it works end to end first:

```bash
kb update --dry-run --limit 5   # what would be sent, and the cost (a few cents)
kb update --limit 5             # 5 items, done in about a minute
kb status                       # records, types, and items still pending
```

Look over the records in `data/state/extracted.jsonl`, then run `kb update` for the rest.

---

## If something goes wrong

The pipeline turns the common problems into a short message that says what to do. Find yours below. If a run fails partway, **nothing in `_state/` changes**, so fixing the problem and running again is always safe.

| You see | What it means | What to do |
| --- | --- | --- |
| `Another run is in progress (held by alex@laptop since …)` | Someone (maybe you) is running it right now, or a run was killed. | If it's someone else, wait and try again; `kb status` shows who holds the lock. See [the lock](#another-run-is-in-progress). |
| `A stale lock was found …` | A run crashed more than 60 minutes ago and left its lock. | `kb update --force-unlock`. |
| `Missing settings: …` / `Config file not found` | `.env` or `config.yaml` is missing a value. | Fill it in ([Quick start](#quick-start-about-5-minutes) steps 3–4). |
| `Service account key not found at …` | The key file isn't where `.env` says. | Save it as `secrets/kb-pipeline.json`, or fix `GOOGLE_APPLICATION_CREDENTIALS`. |
| `Google rejected the service account key` | The key was deleted or disabled in Google Cloud, or your computer's clock is off. | Get the current key from the password manager. If the old one leaked or was rotated, create a new one ([step 2.3](#23-create-a-key)) and update the password manager. |
| `Google couldn't find …` / `Google denied access to …` | A folder ID in `config.yaml` is wrong, or that folder isn't shared with the service account. | Check the ID against the folder's URL. Share the folder with the service account email shown in the message ([step 3](#3-google-drive-access-pattern)). |
| `… can see … but can't change it` | The service account has view-only access to the Knowledge Base folder. | Make it **Content manager** of the Shared Drive. |
| `Google Drive refused to create a file: service accounts have no storage` | The Knowledge Base folder is in someone's My Drive, not a Shared Drive. | Move it into a Shared Drive ([step 3](#knowledge-base-folder-read-and-write)). |
| `The Google … API isn't enabled` | One of the four APIs is off in the Cloud project. | Enable it ([step 2.1](#21-create-a-project-and-enable-the-apis)), wait a minute, run again. |
| `Discord rejected the bot token (401)` | The token in `.env` is wrong, or was reset. | Get the current token from the password manager. |
| `Discord denied access to … (403)` | The bot can't see the channel. | Give the bot **View Channel** and **Read Message History** on it ([step 1.3](#13-give-the-bot-access-to-the-channel)). |
| `The Anthropic API rejected the key (401)` | `ANTHROPIC_API_KEY` is wrong or revoked. | Get the current key from the password manager. |
| `The Anthropic account is out of credit …` | Billing. | Add credit or raise the spend limit in the Anthropic Console, then run again. |
| `… doesn't know that model` | A model name in `config.yaml` is misspelled or retired. | Fix `llm.extract_model` / `llm.synthesis_model`. |
| `Couldn't connect to …` / `timed out` | No internet, or a flaky connection. | Check your connection and run again. |
| `Synthesis or publishing failed: …` (at the end of a run) | Reading and extraction worked and were saved; only the docs step failed. | Fix the cause if the message names one, then `kb update --synthesize`. |
| `kb status` lists **Errors (tried again next run)** | Individual items failed (usually a temporary API error). | Nothing: the next `kb update` retries them. If one keeps failing, check its error. |
| `kb status` lists **Skipped (too large)** | A file is over `llm.max_item_tokens` (about 400 KB of text). | Usually fine to leave. To include it anyway, add its file ID to `drive.allow_large_files` in `config.yaml`. |
| The run summary reports many **rate-limited retries** | Google is throttling. | Lower `drive.export_workers` in `config.yaml` (default 8). |

Anything not in this table is shown with its full traceback. Run it again once (most hiccups are temporary); if it happens again, send the output to whoever maintains the pipeline.

### Another run is in progress

The lock is a small file in `_state/` that says who took it and when. It keeps two runs from writing the state at the same time.

- **Someone else's lock:** their run may still be going, so it's never cleared early. Wait, or ask them. Once it's older than `state.lock_timeout_minutes` (default 60), it counts as stale and `kb update --force-unlock` clears it.
- **Your own lock** (same user and machine, e.g. you closed the laptop mid-run): `kb update --force-unlock` clears it right away. Only do this if no other run of yours is going.

### The docs look wrong

- **A fact or quote is wrong:** follow its citation link (`[C-0042]`) to the source. If the source is right and the record is wrong, the record is in the **Records** tab of the Citations sheet. Synthesis mistakes fix themselves when the section is rebuilt; extraction mistakes stay until the item is extracted again (`kb requeue`, or edit the source so it counts as changed).
- **Feedback someone wants removed:** delete the Discord message (or remove the Drive file from the source folder), then run `kb update --full` (Discord) or `kb update` (Drive). Its citation is marked removed and it leaves the docs.
- **Don't edit the docs by hand.** Each run overwrites them. Comments are fine. Don't edit anything in `_state/` either.

---

## How it works

### What counts as new

- **Discord:** messages after the last one read, in the channel and in each thread (one cursor each). When an old thread gets new messages, only those are extracted, with the earlier ones as context. Bot messages are ignored. Archived **private** threads are skipped unless the bot has Manage Threads.
- **Drive:** every Doc, Sheet, and Slides file in the source folders and their subfolders. A file is new if it wasn't seen before (including old files moved into a source folder) and changed if it was modified since the last run started and its exported text is different (a new comment alone doesn't count). Files that disappear are marked removed. Docs export as markdown with images replaced by `[image]`, Slides as slide text plus speaker notes, Sheets as one table per tab.

### Extraction

Each new or changed item (a Discord thread or channel message, or a Drive file) goes to `llm.extract_model` with the prompt in `prompts/extract_v2.md`. The model returns records, saved in `_state/extracted.jsonl`:

- `feedback`: an opinion, complaint, request, question, or praise, with a category, sentiment, and programming type (or none).
- `program_info`: a `description`, `history`, or `guidance` fact about a programming type.

Every record says what period it's about (`applies_to`: `YYYY`, `YYYY-MM`, or `YYYY-MM-DD`, with `date_basis` saying whether the source stated it, it was inferred, or the source's own date was used), and cites the exact Discord messages or the Drive file. Citations get permanent IDs (`C-0001`, …). Workshops (and other `programming.per_session_types`) also name the specific session. Programming types and sessions found so far are given to the model so it reuses the same names.

- Items over `llm.max_item_tokens` are skipped and reported, unless allowed in config (then they're split into parts but cited as one file).
- A failed request is retried once; an item that still fails is marked `error` and retried next run, without stopping the run.
- Edited Drive files have their records replaced (same citation ID). Removed files and deleted messages keep their records, but their citations are marked removed, so the docs leave them out.

### The docs

After extraction, every saved record is regrouped and both docs are rewritten in place.

- **Consolidated Feedback:** new since the last update, executive summary, then each programming type (most recent feedback first) with what's working, what to improve, and themes. Each theme has a citation count, a breakdown by year, when it was last mentioned, quotes, and every citation. A theme whose feedback stopped after a program change is labeled **likely resolved**. Then themes not tied to one type, feature requests and bugs, open questions, and the citations appendix.
- **Programs:** each type's current description, a dated history (including changes found by comparing sources from different years, citing both sides), how to run it, and what feedback said about each period. Per-session types get a **By year** section with each session.
- **Citations sheet:** a **Citations** tab with every citation (removed ones included) and a **Records** tab with every record.

Synthesis runs `llm.synthesis_model` once per programming type, once per year of sessions for per-session types, once for feedback with no type, and once for the summary. Each section's answer is saved in `_state/synthesis.json` and reused while its records don't change, whoever runs next, so a run that adds one Discord message only redoes that message's type and the summary. The model cites records by ID only; citation links, counts, year breakdowns, and quotes are filled in by code, so they always match the saved records.

Citation links open the Discord message (for server members who can see the channel) or the Drive file (following its sharing settings).

### Models

Extraction and synthesis use separate models (`llm.extract_model` and `llm.synthesis_model`, both Claude Opus 5.5 by default). Extraction mistakes are saved permanently, so it uses the stronger model. Synthesis is rebuilt every time, so it can be switched to a cheaper model at any time: change `llm.synthesis_model` and run `kb update --synthesize`. See [Choosing models](docs/PLAN.md#choosing-models).

### What's in the Knowledge Base folder

| Item | Purpose |
| --- | --- |
| `Consolidated Feedback`, `Programs` | The docs. Share them with the team as Viewer or Commenter. |
| `Citations` | Every citation and every record. |
| `raw/` | The exact text the model was given, per run (Discord messages by day, Drive exports). |
| `_state/` | Cursors, manifest, records, citations, run log, synthesis cache, lock. **Pipeline only, don't edit.** |

The run log (`_state/run_log.json`, shown by `kb log`) records for each run: who ran it (git `user.name`, or `KB_RUN_BY` if set, plus user@machine), the command, the Discord and Drive cursors before and after, each item that was new, changed, or removed and what extraction did with it, and the token and cost summary.

---

## Local data

The pipeline keeps local copies in `data/` so you can inspect exports and debug:

```
data/
├── raw/discord/   # fetched messages per day (+ members.json, cached server nicknames)
├── raw/drive/     # exported text of each Drive file
├── cache/         # LLM answers already received, so a crashed run doesn't pay twice
└── state/         # local copy of _state/ from Drive
```

- `data/` contains other people's messages and documents. It's gitignored; never commit or share it.
- **Drive is the source of truth.** Each run downloads `_state/` and uploads it at the end; your local copy is only a cache. You can delete `data/` at any time.
- Server nicknames are looked up once per author and cached. After people change nicknames, run `kb pull --refresh-names` (or `kb update --full`).

---

## One-time project setup

Done once per project, by whoever owns it. **Already done for LYF**; this is here for reference and for setting up a test copy.

| Step | Who can do it |
| --- | --- |
| Create the Discord bot and add it to the server | Someone with **Manage Server** on the Discord server |
| Create the Google Cloud project and service account | Anyone with a Google account (a managed organization may need an admin) |
| Share the Drive folders with the service account | The owner of each folder, or someone allowed to share it |

Then put the bot token, service account key, Anthropic API key, and the IDs from these steps in the team's password manager for everyone else.

### 1. Create the Discord bot

The pipeline reads the channel through a bot, never a personal account (automating a user account breaks Discord's Terms of Service).

#### 1.1 Create the application and bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and sign in.
2. Click **New Application**, name it (for example, `LYF Knowledge Base`), accept the terms, and click **Create**.
3. Open the **Bot** tab.
4. Under **Privileged Gateway Intents**, turn on **Message Content Intent** and click **Save Changes**. Without it, every message's text comes back empty.
5. Optional: turn off **Public Bot** so only you can add it to servers.
6. Click **Reset Token**, confirm, and copy the token (Discord shows it once). Store it in the password manager; it goes in `.env` as `DISCORD_BOT_TOKEN`.

> Treat the bot token like a password: anyone with it can read everything the bot can. If it leaks, click **Reset Token** again; the old one stops working right away. Then update the password manager and tell everyone who runs the pipeline.

#### 1.2 Invite the bot to the server

1. Open **OAuth2 → URL Generator**.
2. Under **Scopes**, check **bot**.
3. Under **Bot Permissions**, check only **View Channels** and **Read Message History**.
4. Open the generated URL, choose the server, and click **Authorize**.

The bot only reads. It doesn't need to send messages or manage anything.

#### 1.3 Give the bot access to the channel

- **Public channel:** nothing more to do.
- **Private channel:** channel settings → **Permissions** → **Add members or roles**, add the bot, and allow **View Channel** and **Read Message History**.
- **Private threads:** the bot can read one only if it's been added to it. Public threads are readable automatically.

#### 1.4 Get the server and channel IDs

1. In Discord, **User Settings → Advanced** → turn on **Developer Mode**.
2. Right-click the server icon → **Copy Server ID**.
3. Right-click the feedback channel → **Copy Channel ID**.

These go in `config.yaml` as `discord.server_id` and `discord.channel_id`.

### 2. Create the Google Cloud service account

A service account is a Google account that belongs to the pipeline rather than a person, so nobody has to log in to run it and access doesn't break when someone leaves.

#### 2.1 Create a project and enable the APIs

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Project picker → **New Project**, name it (for example, `lyf-knowledge-base`), **Create**, and select it.
3. **APIs & Services → Library**: search for and **Enable** each of:
   - **Google Drive API**: list, export, and upload files
   - **Google Docs API**: read Docs, and fix section links in the published docs
   - **Google Sheets API**: read Sheets, and write the Citations sheet
   - **Google Slides API**: read slide text and speaker notes

These are free at this volume; no billing needed.

#### 2.2 Create the service account

1. **IAM & Admin → Service Accounts → Create Service Account**.
2. Name it `kb-pipeline` and click **Create and Continue**.
3. **Skip** "Grant this service account access to project" (it needs no project roles; all its access comes from Drive sharing), then **Done**.
4. Copy its email, `kb-pipeline@<project-id>.iam.gserviceaccount.com`. You'll share the Drive folders with it.

#### 2.3 Create a key

1. Click the service account → **Keys** → **Add Key → Create new key → JSON → Create**.
2. Save the downloaded file as `secrets/kb-pipeline.json` (gitignored), and store a copy in the password manager.

> **The key works like a password** for the service account: anyone with it can read the source folders and edit the Knowledge Base folder. Never commit, email, or paste it. If it leaks, delete it on the **Keys** tab, create a new one, and update the password manager.

> **"Key creation is disabled"?** Some Google Workspace organizations block keys with the policy `iam.disableServiceAccountKeyCreation`. Ask your Workspace or Cloud admin for an exception for this project.

### 3. Google Drive access pattern

The pipeline reads one or more **source folders** and writes one **Knowledge Base folder**, with different access to each:

```
  Source folders                       Knowledge Base folder
  (existing feedback files)            (what the pipeline writes)
  ┌───────────────────────┐            ┌──────────────────────────────┐
  │ Docs, Sheets, Slides  │   read     │ Consolidated Feedback (Doc)  │
  │ (one or more folders) │ ─────────► │ Programs (Doc)               │
  └───────────────────────┘            │ Citations (Sheet)            │
                                       │ raw/, _state/                │
   service account: Viewer             └──────────────────────────────┘
                                         service account: Content manager
                                         (in a Shared Drive)
```

#### Source folders: read-only

- Share each folder with the service account as **Viewer**: right-click → **Share** → paste the service account email → **Viewer** → turn off **Notify people** → **Share**. Sharing a folder covers everything inside it, including subfolders and files added later.
- The pipeline only lists and exports files here. Viewer access guarantees it can't edit, move, or delete anything, even with a bug.
- Copy each folder ID from its URL (`https://drive.google.com/drive/folders/<FOLDER_ID>`) into `drive.source_folder_ids`.
- If a source folder is in a Shared Drive, add the service account to that Shared Drive as **Viewer**, or share just the folder.

#### Knowledge Base folder: read and write

- **It must be in a Shared Drive.** Service accounts have no Drive storage, so creating files in a normal My Drive folder fails with `storageQuotaExceeded`. In a Shared Drive, files belong to the drive.
  1. Create a Shared Drive (or use an existing one) and create the `Knowledge Base` folder in it.
  2. Shared Drive → **Manage members** → add the service account email as **Content manager**.
  3. Shared Drives need Google Workspace (LYF has it).
- Copy the folder ID into `drive.kb_folder_id`.
- Share the folder, or just the two docs and the Citations sheet, with the team as **Viewer** or **Commenter**. People who run the pipeline don't need Drive access of their own.

#### Rules for the folders

- **Keep them separate.** Never put the Knowledge Base folder inside a source folder or list it as one, or the pipeline reads its own output as new feedback (the smoke test checks for this).
- **Keep access minimal.** Share only these folders with the service account.

---

## Development

```bash
pip install -e '.[dev]'
pytest              # unit tests; no credentials or network needed
ruff check . && ruff format --check .
```

- Code is in `kb/` (`cli.py` is the entry point, `update.py` runs one `kb update`, `errors.py` has the friendly error messages). The repository layout and every design decision are in [docs/PLAN.md](docs/PLAN.md).
- Prompts are in `prompts/` and versioned: change a prompt by adding a new version (`extract_v3.md`), not by editing the old one, so `prompt_version` on saved records stays meaningful.
- New settings go in `config.example.yaml` too, with a comment, and need a default so existing `config.yaml` files keep working.
- To try changes without touching the real docs, point a local `config.yaml` at a test Discord channel and a test Knowledge Base folder.
