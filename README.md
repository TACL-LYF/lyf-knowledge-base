# LYF Knowledge Base

Collects feedback from a Discord channel and a set of Google Drive folders and publishes one **Consolidated Feedback** Google Doc, organized by programming type and theme, with citations for every piece of feedback.

The full design and milestones are in [docs/PLAN.md](docs/PLAN.md).

> **Status:** M1 (raw pulls). `kb pull` reads everything from Discord and Drive into `data/raw/`; the other `kb` commands described below arrive in milestones M2–M4. The setup steps in this README (Discord bot, Google Cloud service account, folder access) can be done now.

## Contents

1. [Before you start](#before-you-start)
2. [Create the Discord bot](#1-create-the-discord-bot)
3. [Create the Google Cloud service account](#2-create-the-google-cloud-service-account)
4. [Google Drive access pattern](#3-google-drive-access-pattern)
5. [Configure your machine](#4-configure-your-machine)
6. [Local data](#local-data)
7. [Running the pipeline](#running-the-pipeline)

## Before you start

The **one-time setup** (steps 1–3) is done once per project by whoever owns it. It needs:

| Step                                                | Who can do it                                                                                   |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Create the Discord bot and add it to the server     | Someone with **Manage Server** permission on the Discord server                                 |
| Create the Google Cloud project and service account | Anyone with a Google account. If your organization manages Google Cloud, you may need an admin. |
| Share the Drive folders with the service account    | The owner of each folder, or someone allowed to share it                                        |

**Each person who runs the pipeline** only does step 4 (configure your machine), using the credentials from steps 1–2.

---

## 1. Create the Discord bot

The pipeline reads the channel through a bot. It never uses a personal Discord account, since automating a user account breaks Discord's Terms of Service.

### 1.1 Create the application and bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and sign in.
2. Click **New Application**, name it (for example, `LYF Knowledge Base`), accept the terms, and click **Create**.
3. Open the **Bot** tab in the left sidebar.
4. Under **Privileged Gateway Intents**, turn on **Message Content Intent** and click **Save Changes**.
   Without this setting, the bot can see that messages exist but every message's text is empty.
5. Optional: turn off **Public Bot** so only you can add the bot to servers.
6. Click **Reset Token**, confirm, and copy the token. Discord only shows it once.
   Store it in a password manager. It goes in `.env` as `DISCORD_BOT_TOKEN` (see [step 4](#4-configure-your-machine)).

> Treat the bot token like a password. Anyone who has it can read everything the bot can read. If it leaks, go back to the **Bot** tab and click **Reset Token**. The old token stops working right away.

### 1.2 Invite the bot to the server

1. Open the **OAuth2** tab, then **URL Generator**.
2. Under **Scopes**, check **bot**.
3. Under **Bot Permissions**, check only:
   - **View Channels**
   - **Read Message History**
4. Copy the generated URL at the bottom, open it in your browser, choose the server, and click **Authorize**.

The bot only reads. It doesn't need permission to send messages, manage anything, or act as an administrator.

### 1.3 Give the bot access to the channel

- **Public channel:** nothing more to do.
- **Private channel:** open the channel's settings → **Permissions** → **Add members or roles**, add the bot, and allow **View Channel** and **Read Message History**.
- **Private threads:** a bot can only read a private thread if it's been added to that thread. Public threads in the channel are readable automatically.

### 1.4 Get the server and channel IDs

1. In Discord, open **User Settings → Advanced** and turn on **Developer Mode**.
2. Right-click the server icon → **Copy Server ID**.
3. Right-click the feedback channel → **Copy Channel ID**.

These IDs aren't secret. They go in your local `config.yaml` (see [step 4](#4-configure-your-machine)).

---

## 2. Create the Google Cloud service account

A **service account** is a Google account that belongs to the pipeline rather than a person. The pipeline signs in with it, so nobody has to go through a Google login when they run it, and access doesn't break when someone leaves the team.

### 2.1 Create a project and enable the APIs

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Open the project picker at the top → **New Project**. Name it (for example, `lyf-knowledge-base`) and click **Create**. Make sure the new project is selected.
3. Go to **APIs & Services → Library**. Search for each of these APIs and click **Enable**:
   - **Google Drive API**: list, export, and upload files
   - **Google Docs API**: read Docs
   - **Google Sheets API**: read every tab of a Sheet, and write the Citations sheet
   - **Google Slides API**: read slide text and speaker notes

These APIs are free for this amount of use. You don't need to add billing.

### 2.2 Create the service account

1. Go to **IAM & Admin → Service Accounts** → **Create Service Account**.
2. Name it `kb-pipeline` and click **Create and Continue**.
3. **Skip** the "Grant this service account access to project" step and click **Continue**, then **Done**.
   The service account needs no Cloud project roles. All of its access comes from Drive sharing (see [step 3](#3-google-drive-access-pattern)).
4. Copy the service account's email address. It looks like
   `kb-pipeline@<project-id>.iam.gserviceaccount.com`. You'll share the Drive folders with this address.

### 2.3 Create a key

1. Click the service account → **Keys** tab → **Add Key → Create new key** → **JSON** → **Create**.
2. A `.json` file downloads. Move it into this repo's `secrets/` folder, which is in `.gitignore`. For example:
   `secrets/kb-pipeline.json`
3. Store a copy in your team's password manager, so other people who run the pipeline can get it there.

> **The key file works like a password** for the service account. Anyone who has it can read the source folders and edit the Knowledge Base folder. Never commit it, email it, or paste it in Discord. If it leaks, delete that key on the **Keys** tab and create a new one.

> **"Key creation is disabled"?** Some Google Workspace organizations block service account keys with the policy `iam.disableServiceAccountKeyCreation`. Ask your Google Workspace or Cloud admin for an exception for this project.

---

## 3. Google Drive access pattern

The pipeline works with **two kinds of Drive folder**: one or more source folders it reads, and one Knowledge Base folder it writes. It has different access to each.

```
  Source folders                       Knowledge Base folder
  (existing feedback files)            (what the pipeline writes)
  ┌───────────────────────┐            ┌──────────────────────────────┐
  │ Docs, Sheets, Slides  │   read     │ Consolidated Feedback (Doc)  │
  │ (one or more folders) │ ─────────► │ Citations (Sheet)            │
  │                       │  pipeline  │ raw/   copies of source text │
  └───────────────────────┘            │ _state/ cursors, manifest... │
   service account: Viewer             └──────────────────────────────┘
                                         service account: Editor
                                         (Content manager in a Shared Drive)
```

### Source folders: read-only

- **What they are:** the existing Drive folders that hold the feedback Docs, Sheets, and Slides. List as many as you need.
- **What's read:** only the files directly inside each folder. Subfolders are skipped for now; reading them is planned for a later milestone. To include a subfolder today, add its ID as another source folder.
- **Access:** share each folder with the service account as **Viewer**. Sharing a folder also gives access to everything inside it, including files added later.
- **What the pipeline does here:** lists files and exports their text. It **never** edits, moves, or deletes anything in these folders. Viewer access guarantees that, even if the code has a bug.
- **How to share:** in Drive, right-click each folder → **Share** → paste the service account email → choose **Viewer** → turn off **Notify people** (the address can't receive mail) → **Share**.
- **Copy each folder ID** from its URL, `https://drive.google.com/drive/folders/<FOLDER_ID>`. They go in `config.yaml` as a list under `drive.source_folder_ids`.

### Knowledge Base folder: read and write

- **What it is:** a new folder that holds everything the pipeline produces:

  | Item                    | Purpose                                | Who uses it                                |
  | ----------------------- | -------------------------------------- | ------------------------------------------ |
  | `Consolidated Feedback` | The output doc                         | The whole team reads it                    |
  | `Citations`             | Running list of every citation         | Anyone checking a source                   |
  | `raw/`                  | Exported text of every source read     | Debugging and audits                       |
  | `_state/`               | Cursors, manifest, saved records, lock | Only the pipeline. **Don't edit by hand.** |

- **Access:** the service account needs **Editor** access, so it can create and update files.
- **Use a Shared Drive if you can.** Service accounts have no Drive storage of their own. In a normal "My Drive" folder, creating new files as the service account fails with a `storageQuotaExceeded` error. In a **Shared Drive**, files belong to the drive instead of the service account, so this works:
  1. Create a Shared Drive (or use an existing one), and create the `Knowledge Base` folder in it.
  2. Shared Drive → **Manage members** → add the service account email as **Content manager**.
  3. Shared Drives need Google Workspace. If you only have personal Google accounts, flag this before M2, because the pipeline would need a different way to write files (for example, signing in as a person for writes).
- **Copy the folder ID** from its URL. It goes in `config.yaml` as `drive.kb_folder_id`.
- **Team access:** share the Knowledge Base folder (or only the Consolidated Feedback doc) with teammates as **Viewer** or **Commenter**. People who run the pipeline don't need any Drive access of their own, since the pipeline acts as the service account.

### Rules for the two folders

- **Keep them separate.** Don't put the Knowledge Base folder inside a source folder, or list it as one. If you do, the pipeline reads its own output as if it were new feedback.
- **Keep them minimal.** The service account should only have access to these folders. Don't share anything else with it.
- **If a source folder is in a Shared Drive,** add the service account to that Shared Drive as **Viewer**, or share only the folder with it.

---

## 4. Configure your machine

Everyone who runs the pipeline does this step.

1. Get the credentials from your team's password manager:
   - the Discord bot token
   - the service account key file (save it as `secrets/kb-pipeline.json`)
   - an Anthropic API key
2. Copy `.env.example` to `.env` and fill it in:

   ```bash
   DISCORD_BOT_TOKEN=...
   GOOGLE_APPLICATION_CREDENTIALS=secrets/kb-pipeline.json
   ANTHROPIC_API_KEY=...
   ```

3. Copy `config.example.yaml` to `config.yaml` and fill in the IDs:

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

`.env`, `config.yaml`, and `secrets/` are in `.gitignore`. Never commit them. The IDs aren't secret, but keeping `config.yaml` local lets each person point at their own channel or folders (for example, a test setup) without changing the repo. If you add a setting, add it to `config.example.yaml` too.

### Install and check access

Needs Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

python scripts/smoke_test.py   # read-only check of Discord and Drive access
pytest                         # unit tests (no credentials needed)
```

The smoke test reads one message from the channel, one from a thread, lists one file in each source folder, and checks that the Knowledge Base folder is in a Shared Drive and writable by the service account. It prints `PASS`/`FAIL` for each check with a hint when something is missing.

---

## Local data

The pipeline keeps local copies of everything it fetches in `data/`, so you can inspect exports and debug without fetching from Discord or Drive again:

```
data/
├── raw/discord/    # fetched messages, per run
├── raw/drive/      # exported text of Drive files
└── state/          # local copy of the _state/ folder from Drive
```

- `data/` is in `.gitignore` and is **never committed**. It contains other people's messages and documents.
- **Drive is the source of truth for state.** Each run downloads `_state/` from Drive into `data/state/` and uploads it again at the end. Your local copy is only a cache.
- You can delete `data/` at any time. The next run downloads state again, and raw files are fetched as needed.

## Running the pipeline

### Raw pull (M1)

```bash
kb pull                   # read the whole channel, its threads, and every in-scope Drive file
kb pull --source discord  # only Discord (or --source drive)
```

This is local only: nothing is written to Discord or Drive. Output goes to `data/raw/`:

```
data/raw/
├── discord/threads/<threadId>.json    one file per thread, including the message that started it
├── discord/messages/<messageId>.json  one file per channel message outside a thread (+ replied-to context)
├── drive/<fileId>.md                  exported text with metadata (title, link, owner, modified time)
└── pull_summary.json                  counts, every item with its link, and any export errors
```

Bot and system messages are left out. Docs are exported as markdown, Slides as slide text plus speaker notes, and Sheets as one table per tab. Only the direct children of each source folder are read for now (subfolders arrive in M2), so list the folders that actually hold the files in `drive.source_folder_ids`. Archived **private** threads are skipped unless the bot has the Manage Threads permission.

### Full pipeline

_(Available from M2 onward.)_

```bash
kb update             # normal run: new Discord messages + new/changed Drive files → rebuild the doc
kb update --dry-run   # show what would change, without calling the LLM or writing anything
kb status             # last run, item counts, errors, programming types found
kb update --full      # rare: read the whole Discord channel again (only for a full reload)
kb rebuild            # rare: extract everything again (e.g., after a big prompt change)
```

The first `kb update` reads everything, because there's no saved state yet. After that, each run only reads what's new.
