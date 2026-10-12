# Backups and upgrades

How StoneSage (and the Hearth app on top of it) keeps your settings and your agents safe, how to upgrade, how to go back, and
how to move to another machine. The same page ships in the StoneSage and Hearth repositories.

Short version: StoneSage backs itself up every night and again before it changes your settings; an upgrade is "get the new
version and restart"; if it goes wrong you restore the backup it took. The one thing you have to do yourself is **copy the
backups off the machine**, because the machine is what you are protecting them from.

## What is a backup

One file, `stonesage-backup-<date>T<time>Z.tar.gz`, owner-readable only, holding what you would miss if the machine died:

| In the backup | Why |
| --- | --- |
| `config.json` | every setting, the login key and every token: without it a restore is a blank install |
| `agents.json` | agents you defined in the cockpit |
| the souls (`souls/`) | each agent's persona, memories and what it learned (plain files) |
| the small state files in the data folder | learned phrasings, custom tools, camera corrections, what Computer already said today, model notes, profile cards, the chat database, and so on |
| the HTTPS certificate and key (`certs/*.pem`), if you made one | so the phones that trust it keep trusting it |

Left out on purpose (the list, with a reason for every entry, is `RULES` in `stonesage/backup.py`): caches that are rebuilt
(spoken-line cache, picture-box cache), recordings of a person's voice, older copies that tools set aside, files somebody was
still writing, the output of the shelved experimental features, and the backups folder itself. A file nobody listed is included
if it is under 5 MB, so a state file added by a later version is not forgotten; anything bigger is skipped and named in the
manifest. Not included, because they are somewhere else: the models (they are downloaded again), the engines' own data, and
services StoneSage only talks to (Home Assistant, Qdrant, Valkey, CouchDB; back those up with their own tools).

**A backup contains your secrets.** There is deliberately no download button and no route that serves one. Treat the file like
`config.json` itself.

## Automatic backups

On by default. The server writes one every day at 03:00 (your time zone), keeps the newest 14, and writes one before it migrates
an old settings file (below). If the machine was off at 03:00, a missed backup is taken five minutes after the next start.

Settings, in `config.json` (defaults shown):

```json
"backup": { "enabled": true, "keep": 14, "hour": 3, "dir": "" }
```

`dir` empty means a `backups` folder inside the data folder. Only files named like backups are ever pruned, and only in that
folder. `GET /api/backup/status` (with your login) shows the last backup (time, size, files, anything that could not be read),
the next run and the last error; `POST /api/backup/run` takes one now.

## Taking one by hand

```
python -m stonesage.backup create                 # into <data folder>/backups
python -m stonesage.backup create --out /mnt/usb  # somewhere else (never pruned)
python -m stonesage.backup list
```

Docker: `docker compose exec stonesage python -m stonesage.backup create`. The backups are in the `/data/backups` folder of the
`stonesage-data` volume; copy them out with `docker compose cp stonesage:/data/backups ./stonesage-backups`.

## Copy them off the machine

Any way you like, as long as it is not the same disk. For example, from another computer:

```
scp -r you@your-server:StoneSage/data/backups ./stonesage-backups      # native install
docker compose cp stonesage:/data/backups ./stonesage-backups          # Docker, run in the folder with docker-compose.yml
```

or a cron job with `rsync`, `rclone` or your NAS's own sync, pointed at the backup folder (set `backup.dir` to a mounted share
if you prefer). Keep copies where only you can read them.

## Upgrading

The layers are upgraded together: Lodestone (models), StoneSage (platform), Hearth (the Home Assistant app). Each says which
version of the one below it was built for ("API 0.1"), and refuses to start with one clear message if the pair does not match, so
a half-upgrade cannot run.

### Docker

```
docker compose pull
docker compose up -d
```

What happens: the new containers start; the cockpit looks at `config_version` in `config.json`; if it is older than the new
version expects it takes a backup first and then runs the settings steps (below), logging each one (`docker compose logs
stonesage`). Your volumes (`stonesage-config`, `stonesage-data`, `models`) are kept.

To stay on a version instead of "latest", set `STONESAGE_IMAGE` (and `LODESTONE_IMAGE`) in `.env` to a version tag, for example
`ghcr.io/lottage/hearth:0.1.0`, and change it deliberately when you want to move.

### Native (Git checkout or pip)

```
python -m stonesage.backup create                  # optional: the server will also make one
git pull                                           # or: pip install -U lodestone stonesage hearth
sudo systemctl restart stonesage                   # or however you start it
```

Upgrade Lodestone, StoneSage and Hearth to matching releases. The server checks on start.

**One data folder.** An install from before October 2026 that runs without `STONESAGE_DATA_DIR` keeps some state (the harness
database, agent DNA, profile cards, the irrigation log) in a second folder, `data` next to the `harness` package. It keeps working
as it is, and backups include that folder. To make it one folder, stop the server and run:

```
python -m stonesage.backup merge-folders --dry-run     # what would move, and which files both folders have
python -m stonesage.backup merge-folders               # asks which copy to keep for each file that differs
```

It takes a backup first, never deletes anything (a replaced copy is kept as `<file>.pre-merge-<time>`, the old folder is renamed
to `data.merged-<time>`), and `--prefer newer|data|harness` answers every question at once.

### Settings upgrades

`config.json` carries `"config_version": N`. When the new version knows more steps than N, then at start, before anything else:

1. a backup is taken (if that fails, nothing is changed and the old settings keep working);
2. the steps run in order; each one renames or moves a setting whose old name used to be read as a fallback (for example
   `garden` became `irrigation`, `hass` became `homeassistant`, the app name `home` became `hearth`), and never throws away a
   value the new place does not already hold;
3. the file is rewritten atomically, owner-only, and each step is logged.

You can look first: `python -m stonesage.config_migrate --dry-run`.

A `config.json` that is **newer** than the program (someone downgraded) is refused: the server prints what happened and exits
without touching anything. Upgrade the program again, or restore an older settings file from a backup.

## Going back

Something wrong after an upgrade? Stop the server, go back to the old version, and restore the backup taken just before.

1. Find the backup: `python -m stonesage.backup list` (the one made by the upgrade has the reason "before settings upgrade").
2. Install the old version: `git checkout <tag>` / `pip install lodestone==<old> stonesage==<old> hearth==<old>` / put the old
   image tag in `.env` and `docker compose up -d`.
3. Restore: `python -m stonesage.backup restore <file>` (add `--dry-run` first to see what it would replace).

A restore checks the archive before it touches anything (format, every checksum, every file name; a link, a path with `..` or an
absolute path is refused), then moves each file it replaces aside as `<name>.pre-restore-<time>` and puts the backed-up one in
its place. It never deletes anything, and files that are in your install but not in the backup are left alone. It refuses while
the server is running unless you pass `--yes`. A backup made by a newer version than the program is refused.

## Moving to a new machine

1. On the old machine take a backup (`python -m stonesage.backup create`) and copy it to the new one.
2. Install StoneSage (and Hearth) on the new machine the usual way, at the same release, and run it once so the folders exist
   (or skip this if you only need the files in place; restore creates what is missing).
3. Stop the server. Restore: `python -m stonesage.backup restore <file>`.
4. Start it. Log in with the same key as before (it is in the restored `config.json`).
5. Check what is address-specific in `config.json`: engine and service addresses (`cluster.*_url`), `api.allowed_hosts` (the
   names you reach the cockpit by), `api.trusted_hosts`, `server.public_url`. Models are not in the backup: the new machine
   downloads or is given them (Lodestone's installer does this).

Docker to Docker: create the containers without starting them (`docker compose create`), copy the file into the data volume
(`docker compose cp ./backup.tar.gz stonesage:/data/restore.tar.gz`), run the restore in a one-off container
(`docker compose run --rm --entrypoint python stonesage -m stonesage.backup restore /data/restore.tar.gz`; the image's normal
entrypoint would start the server instead), then `docker compose up -d`.

## If the server will not start

- "config.json has config_version N, but this StoneSage only understands up to M": the settings are from a newer version. Upgrade
  the program, or restore an older config.
- "was written for Lodestone API X, but the installed Lodestone offers API Y" (or the same for StoneSage): the layers do not
  match. Install the release of the lower layer that goes with the upper one.
- "the backup before it failed ... settings were NOT upgraded": the disk is full or the backup folder is not writable. The old
  settings keep working; fix the cause and restart.
