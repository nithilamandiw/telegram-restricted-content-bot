# Verification record

## Live Telegram checks

**Startup verified on November 17, 2026:** Docker Desktop was started, the bot image
built successfully on this Mac, and both Compose services are running. The bot logged
"Bot ready; one transfer worker is running" after checking shared-account authorization
and connecting to the local Bot API with the configured bot credentials.

**Invite and media flow verified on November 17, 2026:** a supplied controlled invite
joined immediately and was persisted. A second, approval-required invite returned
`INVITE_REQUEST_SENT`. Its post became job #3 in `awaiting_approval`, survived a bot
restart with its deadline intact, and automatically progressed to `done` after the
owner approved the request. Telegram acknowledged the file upload to the existing
requester's bot chat. Temporary job directories were removed.

Before text support was added, an earlier supplied text-only post was rejected as
job #2, with temporary cleanup verified. Live tests used isolated
maintenance runs with the ordinary bot briefly stopped to avoid concurrent session
use; the deployed bot performed approval detection and the successful transfer.

**Text delivery verified on November 17, 2026:** the earlier text-only source was
retried as job #7 after deploying text support. Telegram accepted the copied text
message, the job reached `done`, and no temporary job directories remained. The bot
was restarted successfully after the isolated verification run.

**Message presentation deployed on November 17, 2026:** the running bot was rebuilt
with persistent formatted status cards and inline navigation/cancellation buttons.
Telegram accepted the redesigned HTML welcome message and keyboard in the existing
requester's chat (message #43), and startup logged "Bot ready". Card lifecycle and
callback authorization were verified by the automated tests; no manual button-click
confirmation is recorded.

Protected-media support, large-file delivery, and actual throughput remain unverified.
Automated tests use fake transports for the remaining edge cases.

Use a test channel you control and the configured shared account. Start the bot from
the README, press Start from an allowlisted account, and perform the following checks.
Record the date, server/library versions, size, and observed result locally; do not
include account credentials or private source links in a shared report.

| Case | Expected result | Live result |
| --- | --- | --- |
| Public channel photo | Original downloaded image sent as a document; caption retained | Not run |
| Private channel media | Shared account's access resolves the source and uploads it | Passed after approval, job #3 |
| Public and private topic links | Only the specified message is downloaded | Not run |
| Video/document over 50 MB | Local Bot API sends the complete file; compare checksums | Not run |
| File near the configured 2,000 MB boundary | Streams with bounded memory and completes when disk/network permit | Not run |
| Saving protection enabled on a controlled source | Record whether media download succeeds or Telegram denies it; denied requests report failure | Not run |
| Source inaccessible to shared account | Clear error; no file uploaded | Not run |
| Text-only post | Text copied as messages; no media download | Passed, job #7 |
| Unsupported-media message | Clear error; queue proceeds; temporary files removed | Automated tests |
| Deleted message | Clear error; queue proceeds | Not run |
| Non-allowlisted account | Cannot enqueue, inspect, or cancel jobs | Not run |
| Two allowlisted accounts | Each sees/cancels only its own jobs | Not run |
| Cancel while downloading/uploading | Stops work and removes local temporary files; already-accepted uploads may arrive | Not run |
| Restart during transfer | Job is interrupted, stale files removed, requester invited to retry; no replay | Not run |
| Expired shared session | Owner is directed to terminal login | Not run |
| Six pending jobs from one user | Sixth request rejected with queue guidance | Not run |
| Private invite with immediate membership | Shared account joins and membership is persisted | Passed |
| Invite requiring admin approval | Post waits; approval triggers automatic download | Passed, job #3 |
| Restart while awaiting approval | Original invite and 24-hour post deadline survive | Passed, job #3 |
| Post belongs to a different channel | Mismatch reported; no download | Not run |

Do not repeatedly trigger live flood limits. Rate-limit and disk exhaustion behavior
are simulated in the offline test suite. For a live low-disk test, use an isolated
small filesystem rather than filling the host disk.

## Offline checks

Verified on November 17, 2026 with Python 3.12.16:

- 155 automated tests passed, including PostgreSQL/Heroku support, message presentation,
  text delivery, invite joining and approval waits.
- Ruff lint and formatting checks passed.
- Python byte compilation and installed dependency compatibility checks passed.
- Docker Compose configuration validation passed without a daemon.
- Container build/start subsequently passed; live startup details are recorded above.

Run `python -m pytest` and `python -m ruff check .` in the project environment.
The suite exercises link parsing, private-channel cache resolution, allowlist enforcement,
job ownership, FIFO queues, persistence, limits, byte streaming, safe filenames/captions,
protected-content errors, expired authorization, low disk, bounded downloads,
rate waits, upload ambiguity, cancellation, and restart cleanup. Added tests cover both
invite formats, approval-to-download flow, duplicate requests, per-user invite matching,
paid/invalid/banned/expired invites, migration, waiting limits and expiration, and
cancellation during an approval RPC. Text delivery tests cover literal text and emoji,
web previews, long Unicode text, partial failures, uncertain-send retry handling, and
approval followed by text delivery. These checks never
use your Telegram credentials.

Presentation tests verify a single persistent card through delivery, concurrent updates,
deleted-card replacement, unchanged-message handling, HTML escaping, progress formatting,
and buttons that enforce the allowlist, request ownership and current card ID. Navigation
tests verify per-user status lists and refresh edits.

## Heroku variant

Prepared and locally verified on November 17, 2026. The full suite passed against a
disposable PostgreSQL 17 instance as well as the default SQLite backend. PostgreSQL
checks covered reconnect persistence, 64-bit IDs, invitation and status-card state,
approval recovery, cancellation, transaction rollback, concurrent queue limits, and
exclusive worker locking. Session-export and process-supervision tests also passed.

`Dockerfile.heroku` built successfully for linux/amd64. A smoke test with dummy app
credentials and no bot token verified Python 3.12.16, Psycopg 3.3.9, module imports,
and the native local API listener running as arbitrary non-root UID 12345. The test
PostgreSQL container and its temporary database volume were removed afterwards.

No Heroku app, database add-on, or worker was created. Actual cloud startup and
large-file performance remain unverified. The local Telegram bot remains stopped.
Deployment and cutover instructions are in [heroku.md](heroku.md).

Compose syntax can be checked without a Docker daemon or a real `.env` file:

```sh
docker compose --env-file .env.example config --no-env-resolution --quiet
```
