# Telegram Restricted Content Bot

A private bot for an allowlist of Telegram users. Send a message link; the bot reads it
through one shared user account and returns its text as a message or its media as an
original document. Python 3.12, Telethon, aiogram, SQLite, and a local Telegram Bot API server.

- Public channel, private channel/group, and topic message links.
- Text posts with line breaks and emoji; long text is split into ordered messages.
- Private invite joining, with automatic download after admin approval (up to 24 hours).
- Files up to **2,000,000,000 bytes**, without loading the whole file into memory.
- One transfer at a time; five pending requests per user, including the active request.
- Progress, cancellation, persistent job history, restart recovery, and temporary-file cleanup.
- Formatted status cards, progress bars, and buttons for Cancel, Open post, and My requests.

Only the linked message is processed. There is no bulk download,
web dashboard, or per-user account login. Everyone on the allowlist uses the shared
account's access to **all accessible channels and groups**. Private user conversations
are excluded. Choose your allowlist accordingly.

## Protected posts

The implementation downloads media bytes and uploads a new file; it does not forward
the source message. This does **not guarantee** that saving-protected posts can be
downloaded. Telegram [documents restrictions on protected content](https://core.telegram.org/api/content-protection).
If a media request is denied, the bot reports a failure instead of claiming success.
The bot does not change channel protection settings.

Live protected-content behavior has **not been verified** in this workspace. Run the
controlled checks in [docs/verification.md](docs/verification.md) with your account
before relying on that capability.

## Start with Docker Compose

Requirements: Docker with Compose and a running Docker daemon, Telegram access, and
enough disk for a downloaded file plus the Bot API server's temporary/cache files.

1. Create a bot through [@BotFather](https://t.me/BotFather). Obtain your application
   `api_id` and `api_hash` at [my.telegram.org/apps](https://my.telegram.org/apps).
   The shared account can join private sources when an allowlisted user sends an invite.
2. Copy the example configuration:

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

   Edit `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `BOT_TOKEN`, and `ALLOWED_USER_IDS`.
   The allowlist uses numeric **user IDs**, not usernames, phone numbers, or channel IDs.
   No credentials, verification codes, or session files need to be sent to anyone.
3. Build and log in to the shared account interactively:

   ```sh
   docker compose build bot
   docker compose run --rm --no-deps bot python -m telegram_downloader check
   docker compose run --rm --no-deps bot python -m telegram_downloader login
   ```

   Enter the account phone number, verification code, and two-step verification password
   in your terminal. Codes and passwords are hidden. The account session is saved in
   `./data/account.session`. This is a user account login, separate from the bot token.
4. If the bot previously ran against `api.telegram.org`, stop that instance and run this
   **once**, before using the local server:

   ```sh
   docker compose run --rm --no-deps bot python -m telegram_downloader logout-cloud
   ```

   Skip this for a new bot. Telegram requires [logging out of the hosted API when moving
   to a local server](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server).
   Do not run this command at every restart or run competing bot instances.
5. Start the services:

   ```sh
   docker compose up -d
   docker compose logs -f bot
   ```

   The bot retries initial API connection failures and restarts if the local server is
   still starting. The API endpoint has no published port. No inbound public port or
   webhook is required.
6. Open the bot in Telegram, press **Start**, and send one message link:

   ```text
   https://t.me/channel_name/123
   https://t.me/c/1234567890/123
   https://t.me/c/1234567890/10/123
   https://t.me/channel_name/10/123
   ```

   The last number identifies the message; an earlier number can identify its topic.
   `?single` links and public `/s/` preview links are accepted. Discussion
   comment links, multiple links in one request, and non-Telegram URLs are rejected.

   For a private source the shared account has not joined, first send its invite:

   ```text
   https://t.me/+INVITE_HASH
   https://t.me/joinchat/INVITE_HASH
   ```

   The bot joins using the shared account or submits a join request. Then send the
   media-post link. An inaccessible post uses **your latest submitted invite**; after
   membership is approved, the bot verifies that the post belongs to that channel.
   Already-accessible sources download normally, even if another invite is pending.
   Paid invitations are rejected without making payments.

## Commands and behavior

Each new request has one status card that updates in place from queue to delivery,
including waiting, errors, and cancellation. Files show their name and a progress bar
with transferred size. Status card IDs are saved in SQLite so approval notifications
and restart recovery can update the same card. Deleted cards are replaced when needed.
Use **Cancel** on a pending card, **Open post** to view the source, or **My requests**
to open a private status list with a **Refresh** button. The welcome and invite messages
use the same formatting; original source text and file captions keep their existing behavior.

| Input | Result |
| --- | --- |
| `/start` or `/help` | Instructions |
| One message link | Queue delivery of its text or media |
| One private invite link | Join, or request admin approval, using the shared account |
| `/status` | Your ten most recent jobs and failure reasons |
| `/cancel 12` | Cancel your pending job #12 |
| `/cancel` | Cancel all your pending jobs |

Only allowlisted users in a private bot chat can use the bot. Status and cancellation
are restricted to the requester's own jobs. Edit the allowlist in `.env` and recreate
the bot container to apply changes:

```sh
docker compose up -d --force-recreate bot
```

Uploads are documents to preserve the downloaded bytes and filename. Videos may
therefore appear as files rather than inline players. Captions are plain text, bounded
to Telegram's 1,024 UTF-16-unit limit. Text-only posts are copied as plain messages,
preserving their text and line breaks; source formatting entities are not reproduced.
Long text is split into parts of at most 4,096 UTF-16 units without cutting Unicode
characters. Web-preview posts send their text rather than a preview image, with new
previews disabled. Unsupported attachments still produce an explanatory error;
linked album members are processed individually.

Short account and bot flood waits are respected, with up to three attempts. Account
waits longer than five minutes fail the job so the queue can continue. Temporary
download connection failures are retried from the beginning. Upload connection
failures are **not automatically retried**, since a lost response can follow successful
delivery. Text deliveries follow the same rule; after a partial long-text failure,
the error reports that earlier parts already arrived. Check the chat before retrying
an uncertain delivery or an interrupted job.
Cancellation during an upload cannot retract a file Telegram has already accepted.

Approval-required posts appear as `awaiting_approval` in `/status`. The bot checks
membership every 60 seconds, respects Telegram flood waits, and resumes the download
automatically. Each waiting post expires 24 hours after submission. These requests
count toward the same five-pending-jobs limit but do not block other downloads.
Duplicate invites reuse the existing join request. Cancelling a waiting download
stops its checks; it does not leave the channel or withdraw a submitted join request.

On restart, ordinary queued and active jobs are marked `interrupted`, stale job directories are
removed, and still-allowlisted requesters are asked to resubmit. Jobs are never silently
replayed. Waiting-for-approval jobs are preserved and resume checking with their original
invitation association and deadline. Graceful shutdown also cleans the active download.
`/status` retains the interruption even when its notification cannot be delivered.

Updating an existing installation automatically adds invitation tables and job fields
to SQLite without deleting history. Rebuild only the bot to deploy code changes:

```sh
docker compose up -d --no-deps --build bot
```

## Configuration and storage

| Variable | Default / purpose |
| --- | --- |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Required application credentials |
| `BOT_TOKEN` | Required bot token |
| `ALLOWED_USER_IDS` | Required comma-separated positive user IDs |
| `DATA_DIR` | `./data`; Compose overrides to `/data` |
| `BOT_API_BASE_URL` | `http://127.0.0.1:8081`; Compose overrides to `http://bot-api:8081` |
| `MAX_FILE_BYTES` | `2000000000`; may be lowered |
| `MAX_PENDING_PER_USER` | `5`, including the active job and posts awaiting approval |
| `MIN_FREE_DISK_BYTES` | `67108864`; disk reserve for downloads |
| `TRANSFER_TIMEOUT_SECONDS` | `7200`; total time for a transfer |
| `PROGRESS_INTERVAL_SECONDS` | `5`; minimum interval between progress edits |
| `BOT_API_IMAGE` | `aiogram/telegram-bot-api:latest`; set an image digest for a fixed deployment |

The local server is necessary for [uploads up to 2,000 MB](https://core.telegram.org/bots/api#using-a-local-bot-api-server).
Pointing at the hosted API is rejected. Docker uses the
[aiogram-maintained Bot API image](https://github.com/aiogram/telegram-bot-api), an unofficial
image of Telegram's server. Python dependencies, including transitive dependencies,
are pinned with hashes in `requirements.lock`; development tools are in
`requirements-dev.lock`. The image tag and Python base image are intentionally
configurable/updateable; pin a digest in your deployment if immutability is required.

`./data` contains the shared account session, SQLite history, and temporary downloads.
It is excluded from Git and Docker build context, as is `.env`. New files use restrictive
permissions. Treat session files as account credentials. The Bot API has a separate
named volume, `bot-api-data`, for its state/cache. It may retain cached files independently
of the application's temporary cleanup; monitor Docker disk usage as well as `./data`.

Run only one bot/login process per data directory. A process lock prevents concurrent
session use on one host. Before re-login, stop the bot:

```sh
docker compose stop bot
docker compose run --rm --no-deps bot python -m telegram_downloader login
docker compose up -d bot
```

Stop with `docker compose down`. Data remains available for the next start. Back up
account and database files while the bot is stopped. Avoid deleting volumes unless
you intend to remove the local Bot API's state.

## Native development and tests

Use Python 3.12 or newer:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

The tests use fake transports and temporary SQLite databases; they do not log in to
Telegram or send real messages. For native execution, run a local Bot API server on
loopback port 8081, configure `.env`, then use:

```sh
.venv/bin/python -m telegram_downloader login
.venv/bin/python -m telegram_downloader run
```

Keep the local API private to your machine/container network. A remote endpoint needs
your own secure transport. The process logs job IDs and exception class names rather
than credentials, source links, raw Telegram errors, or token-bearing request URLs.

The live validation checklist and its current status are in
[docs/verification.md](docs/verification.md).

## Heroku deployment

For Heroku's **Cedar container stack**, use [docs/heroku.md](docs/heroku.md).
`Dockerfile.heroku` bundles the private Bot API server and bot in one worker dyno;
`heroku.yml` supplies the worker definition. This variant uses `DATABASE_URL` for
PostgreSQL and `TELEGRAM_SESSION` for the exported account session instead of relying
on persistent local files. Install its additional pinned dependencies from
`requirements-heroku.lock` when running that variant outside the image.

For GitHub/browser deployment, `app.json` selects the container stack and collects
configuration values. The worker initially stays off while you attach Postgres and
choose its plan. The guide includes the browser deployment URL and update workflow.

Export an existing login with `python -m telegram_downloader export-session`; the
secret is saved privately under `data/`. The guide covers Config Vars, linux/amd64
image builds, switching off the old local API server, and Heroku worker startup.
Local Docker Compose continues to use SQLite and its existing session file when
the cloud-only variables are unset. No Heroku account or paid resources are created
by these files or by the local tests.
