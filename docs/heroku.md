# Heroku Docker deployment

This deployment runs the Telegram bot and a private local Bot API server inside one
worker dyno. Request history, invitation associations, status-card IDs and approval
deadlines live in PostgreSQL. The user account is restored from `TELEGRAM_SESSION`.
Temporary downloads and the API server cache use the dyno's temporary filesystem.

Use a **Cedar-generation app on the `container` stack**. Heroku's direct Docker
deployment path and `heroku.yml` are supported on Cedar; Fir requires Cloud Native
Buildpacks. See [Heroku generations](https://devcenter.heroku.com/articles/generations)
and [Docker deployment requirements](https://devcenter.heroku.com/articles/container-registry-and-runtime).

The files do not provision an app, paid dyno, or database automatically. Select your
worker and Heroku Postgres plans in your own account. No web dyno or public port is
needed. Keep exactly **one worker**. A PostgreSQL advisory lock rejects duplicate
workers using the same database.

## GitHub and browser deployment

You can upload the project to GitHub and let Heroku build the Docker image. Manual
`docker push` is not needed for this route. Include `app.json`, `heroku.yml`,
`Dockerfile.heroku`, `requirements-heroku.lock`, `pyproject.toml`, and the full
`telegram_downloader/` package. Keep `.gitignore` and `.dockerignore` in the repo.
Do not upload `.env`, `data/`, session exports, local databases or virtual environments.
GitHub's web upload does not apply `.gitignore` rules for you; select source files only.

After exporting the session as described below and uploading the source, open this
URL in your browser, replacing the GitHub username and repository name:

```text
https://heroku.com/deploy?template=https://github.com/YOUR_USERNAME/YOUR_REPOSITORY
```

The included `app.json` selects the `container` stack and prompts for the required
Config Vars. It starts with **zero worker dynos** and does not provision a database.
After the build succeeds, add Heroku Postgres in **Resources** and select a plan;
confirm that `DATABASE_URL` appears in Settings. Complete the old-server cutover below,
then select a worker plan and enable exactly one worker in **Resources**. Open the
Telegram bot to test it; this worker-only app does not serve a website.

For a private GitHub repository, link your GitHub account to Heroku and grant it repo
access. Heroku's [Deploy button documentation](https://devcenter.heroku.com/articles/heroku-button)
explains private-repo access. To deploy later updates from the dashboard, use
**Deploy → GitHub → Connect repository → Manual deploy → Deploy branch**; the app
must retain the `container` stack so Heroku uses `heroku.yml`.

The remaining CLI image-upload steps are an alternative route. Session preparation,
Config Vars, PostgreSQL, and switching off the old API server apply to both routes.

## Prepare the Telegram session locally

Finish pending transfers and stop the existing bot first:

```sh
docker compose stop bot
```

If the account is already logged in, build the updated local image and export its
session without printing the secret:

```sh
docker compose build bot
docker compose run --rm --no-deps bot python -m telegram_downloader export-session
```

The command creates `./data/telegram-session.txt` on your Mac with private permissions. It refuses
to overwrite an existing file. If needed, choose another private file under `data/`
using `--output data/telegram-session-new.txt`. If there is no authorized session,
run the existing terminal `login` command first.

Copy the exported file's contents directly into the **TELEGRAM_SESSION** config var
in Heroku's Settings. Keep the value private; it grants access to the shared account.
Do not commit it or bake it into an image. The export reads the local session and
does not log out the account or contact Telegram.

## Create and configure the app

Install the Heroku CLI, log in, and create a container-stack app in your account:

```sh
heroku login
heroku create YOUR_APP_NAME --stack container
```

For an existing Cedar app, use `heroku stack:set container --app YOUR_APP_NAME`.
Attach a Heroku Postgres database using the dashboard and select the plan there.
It supplies `DATABASE_URL` automatically. Remote PostgreSQL connections require TLS.

Set these Config Vars through the dashboard:

| Variable | Value |
| --- | --- |
| `TELEGRAM_API_ID` | Your existing application ID |
| `TELEGRAM_API_HASH` | Your existing application hash |
| `BOT_TOKEN` | Your existing bot token |
| `ALLOWED_USER_IDS` | Existing comma-separated numeric user IDs |
| `TELEGRAM_SESSION` | Contents of the exported session file |
| `DATABASE_URL` | Automatically supplied by Heroku Postgres |

`DATA_DIR=/tmp/telegram-downloader` and the loopback Bot API URL are configured in the
image. Keep these defaults. Existing size, queue and timeout environment settings
remain available. The bundled server binds only to `127.0.0.1:8081` and uses `--local`
for the existing large-file upload support.

Heroku starts a new request history in PostgreSQL; this update does not copy the
Mac's SQLite history into it. Telegram channel memberships remain part of the shared
account. Complete or cancel old pending requests before switching deployments.

## Build and push the image

Run from the project directory. Heroku's container runtime needs **linux/amd64**,
including when building on an Apple Silicon Mac. Disable build provenance to keep
the registry image in the supported manifest format:

```sh
docker build --platform linux/amd64 --provenance=false -f Dockerfile.heroku -t registry.heroku.com/YOUR_APP_NAME/worker .
heroku container:login
docker push registry.heroku.com/YOUR_APP_NAME/worker
```

The image inherits the aiogram-maintained Telegram Bot API image and installs its
Python runtime and the hashed dependencies from `requirements-heroku.lock`. The base
image can be fixed with `--build-arg BOT_API_IMAGE=aiogram/telegram-bot-api@sha256:...`.
The supplied `heroku.yml` provides the same worker build/run definition for a
Heroku-hosted Docker build when using Git deployment.

## Switch from the old local API server

Telegram recommends logging the bot out of the old local server before moving it.
This is separate from the **user account** session export and does not log out that
account. See [Telegram's local-server migration instructions](https://github.com/tdlib/telegram-bot-api#moving-a-bot-from-one-local-server-to-another).

Build the updated local app image to make the migration command available, then
run it against the existing local API server while the bot itself is stopped:

```sh
docker compose build bot
docker compose run --rm --no-deps bot python -m telegram_downloader logout-local
docker compose stop bot-api
```

If the old server is elsewhere, run `logout-local` against that server's configured
API endpoint. Do this during the actual cutover, not as a routine restart command.
Do not run the Mac bot and Heroku bot at the same time.

Release the worker image and select a suitable worker plan before scaling it on:

```sh
heroku container:release worker --app YOUR_APP_NAME
heroku ps:scale worker=1 --app YOUR_APP_NAME
heroku logs --tail --app YOUR_APP_NAME
```

Wait for `Bot ready`, then send `/start` and a post link in Telegram. Test one ordinary
file, one text post, and an approval-required channel before relying on the deployment.
For a new bot that previously used the hosted API, use the existing `logout-cloud`
procedure instead of `logout-local` before starting the local API instance.

## Restarts, resource limits and stopping

Heroku's [filesystem is temporary](https://devcenter.heroku.com/articles/dynos#ephemeral-filesystem).
Request and approval state survive in PostgreSQL. Waiting jobs retain their original
deadline; queued/active transfers interrupted by a restart require resubmission.
Downloaded files and the API cache are not persisted across dyno replacement.
The supervisor handles SIGTERM and stops both processes; if either component fails,
it exits so Heroku can restart the worker.

Large-file delivery still depends on the dyno's available temporary disk, memory,
CPU and the transfer timeout. The code streams file chunks, but an in-flight transfer
can be interrupted by a platform restart. Two-gigabyte uploads have not been verified
on a Heroku dyno. The project does not run an actual cloud deployment during local tests.

Stop the hosted bot with:

```sh
heroku ps:scale worker=0 --app YOUR_APP_NAME
```

## Local verification of the deployment variant

Build the image and inspect it as an arbitrary non-root UID without logging in:

```sh
docker build --platform linux/amd64 --provenance=false -f Dockerfile.heroku -t telegram-downloader-heroku:local .
docker run --rm --platform linux/amd64 --user 12345:12345 telegram-downloader-heroku:local python --version
```

Install `requirements-dev.lock` and `requirements-heroku.lock` into a test environment,
then run `python -m pytest`. PostgreSQL integration tests require a dedicated disposable
database ending in `_test`, selected through **TEST_DATABASE_URL**. Those tests truncate
the test database's bot tables. They never use `DATABASE_URL` implicitly.

The prepared files were locally verified on November 17, 2026: 155 tests passed
against SQLite and a disposable PostgreSQL 17 database; the Heroku image built as
linux/amd64; Python 3.12.16, Psycopg 3.3.9, and the bundled API listener worked as UID
12345 without root privileges. No real bot token was used for the image smoke test.
An actual Heroku app has not been provisioned or deployed by this work.
