# Hearth

Hearth is an app for the [StoneSage](https://github.com/lottage/stonesage) agent platform that runs a house through
Home Assistant. Its assistant is called **Computer**: a dry, sarcastic, British attic computer that is loyal all the
same, and actually does things. It reads and controls devices, looks through the cameras, knows who is home, and
speaks through smart speakers.

## Where it fits

Hearth is layer 2 of a three-layer pyramid. Dependencies point down only: StoneSage and Lodestone never import Hearth;
Hearth registers its tools, routes and hooks with the platform when `config.json` lists it under `"apps"`.

| Layer | Repository | Job |
| --- | --- | --- |
| 2 | **Hearth** (this repository), [Grove](https://github.com/lottage/grove) | Apps: a Home Assistant house assistant; an optional agents add-on |
| 1 | [StoneSage](https://github.com/lottage/stonesage) | Agent platform: souls, tools, approvals, memory |
| 0 | [Lodestone](https://github.com/lottage/lodestone) | Serves the models |

## What is in it

- **Computer** (`persona`, `household`): the persona and the household it serves (people, pets, cameras, phones,
  speakers) are read from `config.json`; nothing about a particular home is written in code.
- **Home Assistant** (`hass_client`, `ha_tools`, `ha_policy`, `reflex`): tools to read and control entities, limits on
  what may be called, and plain on/off orders that skip the language model. An action you ordered outright runs at
  once; one that Computer inferred, and anything that unlocks or opens, asks first.
- **Presence and cameras** (`frigate_presence`, `presence_corrections`, `sighting_boxes`, `patrol`, `camera_ui`,
  `ha_presence`): who and what is in view from a Frigate event stream, a vision model for sweeps of pan-tilt cameras,
  corrections from a person, and presence published back to Home Assistant.
- **Unprompted remarks** (`commentary`): when Computer speaks without being asked, under a daily budget and with
  rules about when someone could be woken.
- **Voice** (`voice_client`, `tts_cache`, `voice_bench`, `alexa_relay`): speech in and out, cached confirmations, a
  recorder bench for choosing a recogniser, and a relay that lets an Alexa skill reach Computer without exposing the
  house to the internet.
- **Other** (`adguard_blocks`, `immich_client`): a one-touch content block through AdGuard Home, and a photo client.
- `deploy/`: things you run elsewhere. `alexa-skill/` builds the Alexa skill, `voice-server/` is a speech-to-text and
  text-to-speech server with Wyoming endpoints for Home Assistant, `cluster-bridge/wildlife_admin.py` applies
  presence corrections on the camera host.

## Security

Hearth runs on StoneSage's login (see the Security section of the [StoneSage README](https://github.com/lottage/stonesage)):
one server key, a session cookie for browsers, a bearer for scripts, no cross-origin access.

- **Home Assistant's Ollama integration** cannot send a header. Put Home Assistant's address in `api.trusted_hosts`; that
  machine then reaches only `/api/chat`, `/api/tags`, `/api/version`, `/api/show` and `/metrics`.
- **Locks, garage doors and alarms always ask.** Computer asks for a yes before unlocking or opening, and the cockpit's own
  service route refuses locks, covers, valves, sirens and alarm panels outright. The raw Home Assistant service route
  (`/api/hass/call`) does not exist unless `features.raw_ha_call` is true.
- **The Home Assistant token never leaves its address.** It is never returned by the API, and changing the Home Assistant URL
  (in the setup wizard or the settings) requires the token to be pasted again.
- **Camera frames, presence and chats** are behind the login like everything else; the Alexa relay is outbound-only and
  sealed with a shared key that the settings API masks.

### What you need first

A clean machine needs these once, before the commands below.

**Python 3.10 or newer with `venv`, and git.** pip fetches the layers from GitHub, so even a pip install needs git. Modern
Linux (Ubuntu 24.04 among them) refuses `pip install` outside a virtual environment, so make one:

- Ubuntu / Debian: `sudo apt install git python3-venv python3-pip`, then
  `python3 -m venv ~/stonesage && . ~/stonesage/bin/activate`
- Windows (PowerShell): install Python 3.10+ and Git for Windows, then
  `py -m venv $HOME\stonesage; & $HOME\stonesage\Scripts\Activate.ps1`
  (if PowerShell refuses the script: `Set-ExecutionPolicy -Scope Process RemoteSigned`, then repeat)
- macOS: `brew install python git` (or the python.org installer), then
  `python3 -m venv ~/stonesage && . ~/stonesage/bin/activate`

**For the Docker install: Docker Engine, the Compose plugin and Buildx.** Docker Desktop (Windows, macOS) has all three. On
Ubuntu or Debian use Docker's own apt repository (`docker-ce docker-ce-cli containerd.io docker-buildx-plugin
docker-compose-plugin`), or Ubuntu's `docker.io` plus `docker-buildx` and `docker-compose-v2`: `docker.io` alone has no Buildx,
and the legacy builder it falls back to is deprecated and can fail. Compose 2.20 or newer (`docker compose version`). For an
NVIDIA GPU also the NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/); for an AMD or Intel GPU,
Linux with `/dev/dri`.

## Install with Docker

Docker works the same on Linux, Windows and macOS. Hearth's dependencies include Lodestone, whose installer writes the
Compose files for the whole stack: language models, a gateway in front of them, StoneSage with Hearth, and a model
download on the first start.

```
pip install "git+https://github.com/lottage/hearth@v0.1.0-rc.7"   # brings Lodestone and StoneSage along, at the same release
pip install --upgrade "git+https://github.com/lottage/hearth@v0.1.0-rc.7"   # an existing install moves to this release (all three)
python -m lodestone.installer --docker                        # looks at the machine, writes docker-compose.yml and .env here
# the installer wrote your API keys into .env; HASS_TOKEN (a Home Assistant long-lived access token) can stay empty: the setup wizard asks for it. Then
docker compose up -d --build
```

**Images.** While a release has no published images (every pre-release, such as v0.1.0-rc.7) the installer writes `build:`
sections: Compose builds the layers' images itself from GitHub at that tag (no clone needed; the first start takes a few
minutes longer), then downloads the models. For a release with published images the file names them by version instead
(`ghcr.io/lottage/<layer>:0.1.0-rc.7`, never `latest`). `--build` / `--no-build` override the choice; the one command,
`docker compose up -d --build`, works either way.

First start, read this once: stock Ubuntu needs `docker-buildx` (Docker's own repository has it as `docker-buildx-plugin`)
before the build works, and the installer needs `python3-venv` and `python3-pip` (the step above). Leave `HASS_TOKEN=` empty
in `.env` if you do not have the token yet: the setup wizard in the cockpit asks for it. The first start downloads several GB
of base images, then the models. `models` and `stonesage-image` are one-shot containers, so `docker compose ps` shows them
as "Exited (0)" once they are done, by design; the other services should show "healthy" or "running".

The cockpit is on `http://localhost:8888` (log in with `STONESAGE_API_KEY` from `.env`) and the models' OpenAI-compatible
address on `http://localhost:8000/v1` (send `LODESTONE_API_KEY` as a bearer), both bound to this computer only until you
change `STONESAGE_BIND` / `LODESTONE_BIND` in `.env`; to reach the cockpit by a host name, list it in `STONESAGE_ALLOWED_HOSTS`. On the first start
`config.json` is written into the `stonesage-config` volume from the environment (Hearth enabled, the engines as
`cluster.*_url`, your Home Assistant URL and token); after that it is yours to edit in the cockpit. Describe the house
there (`household`, `infra_words`, see below). Without Python, `docker/docker-compose.yml` and `docker/.env.example` in
this repository are a ready CPU stack.

**Home Assistant address and household, on the Docker path.** `.env` carries `HASS_URL` (the installer writes the placeholder
`http://homeassistant.local:8123`; set it to an address the *container* can reach, such as `http://<home-assistant-host>:8123`, because
`localhost` there means the container itself) and `HASS_TOKEN` (a long-lived access token, may stay empty). They are copied into
`config.json` on the **first start only**; after that the cockpit owns them. The setup wizard (Smart Home tab, F5) asks for **both**
the URL and the token, and also for the household, so you do not have to edit files: `household.people`, `pets` (name to species),
`owner_pronouns`, `cameras`, `ptz_cameras`, `default_camera`, `phones`, `speakers`, `descriptions`, and `infra_words`. The wizard is
complete when it has the URL, the token and at least one person.

**Talking to Computer from other software.** StoneSage serves an OpenAI-compatible API, so a client can reach Computer directly:
`POST /v1/chat/completions` with `"model": "computer"` (an alias of the agent id `courage-computer`, which works too) and
`Authorization: Bearer <STONESAGE_API_KEY>`. `GET /v1/models` lists the agents. It runs Computer's whole tool loop (house tools,
approvals), not the bare language model; the bare model is the gateway on port 8000.

| Docker on | CPU | NVIDIA GPU | AMD or Intel GPU |
| --- | --- | --- | --- |
| Linux (x86-64 or arm64) | yes | yes, with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) | yes, through `/dev/dri` (Vulkan) |
| Windows (Docker Desktop, WSL2) | yes | yes, with a current NVIDIA driver | no |
| macOS (Docker Desktop, Intel or Apple Silicon) | yes | no | no |

CPU is the default when no usable GPU is visible, so the same command works everywhere (give Docker Desktop at least 8 GB
of memory for it). Expect roughly 20-40 tokens per second for the small model on a modern desktop CPU and only a few on an
older server CPU: fine for chat, slow for Computer's voice use, for which a GPU is strongly recommended (waits for the model
are lengthened automatically on a CPU stack). `--gpu vulkan|cuda|cpu`, `--profile auto|gpu-24gb|gpu-16gb|gpu-12gb|gpu-8gb|gpu-6gb|gpu-4gb|gpu-2gb|cpu`
(`python -m lodestone.installer --list-profiles` shows the ladder: which card gets which models), `--vision`, `--out DIR` and
`--build` / `--no-build` adjust the plan. The installer names the card it found and the rung it chose
(`found Quadro M2000, 4.0 GB -> gpu-4gb`). The Hearth image is the StoneSage image plus this app (`Dockerfile` here,
`FROM` the StoneSage image).

The Docker path has been run end to end on an NVIDIA GPU (CUDA, a Quadro M2000 with 4 GB, 2026-10-11) and on a CPU-only
stack (2026-10-12). The AMD / Intel path through `/dev/dri` (Vulkan), and most rungs of the GPU ladder
(`python -m lodestone.installer --list-profiles`), are tested offline only: the generated files are checked against the
rules the engines need, not run.

## Add-on: Grove

[Grove](https://github.com/lottage/grove) is an optional add-on: agents with no task that write, read the web and keep a
research record. It is the same install with one more package and one more flag:

```
pip install --upgrade "git+https://github.com/lottage/hearth@v0.1.0-rc.7" "git+https://github.com/lottage/grove@v0.1.0-rc.7"
python -m lodestone.installer --docker --with-garden
```

Then `docker compose up -d --build`. The [Grove README](https://github.com/lottage/grove) has the firewall steps (the reader's
network is closed by a script you run as root), how to start a session and how to turn the scheduler on.

The setup wizard (the cockpit's first page) is **complete** when it has the Home Assistant URL, the Home Assistant token and at
least one household person (`household.people`). Phones, speakers and cameras are suggested, not required.

## Install without Docker (bare metal)

### What you need first

A clean machine needs these once, before the commands below.

**Python 3.10 or newer with `venv`, and git.** pip fetches the layers from GitHub, so even a pip install needs git. Modern
Linux (Ubuntu 24.04 among them) refuses `pip install` outside a virtual environment, so make one:

- Ubuntu / Debian: `sudo apt install git python3-venv python3-pip`, then
  `python3 -m venv ~/stonesage && . ~/stonesage/bin/activate`
- Windows (PowerShell): install Python 3.10+ and Git for Windows, then
  `py -m venv $HOME\stonesage; & $HOME\stonesage\Scripts\Activate.ps1`
  (if PowerShell refuses the script: `Set-ExecutionPolicy -Scope Process RemoteSigned`, then repeat)
- macOS: `brew install python git` (or the python.org installer), then
  `python3 -m venv ~/stonesage && . ~/stonesage/bin/activate`

Hearth depends on Lodestone and StoneSage (neither is on PyPI); pip fetches both from GitHub, at the same release, as part
of the install:

```
git clone --branch v0.1.0-rc.7 https://github.com/lottage/hearth
cd hearth
pip install -e .
```

For the models, `sudo python -m lodestone.installer --apply` sets up llama.cpp engines and a gateway on a fresh Ubuntu
24.04 machine (run it without `--apply` first to see the plan). Then run StoneSage as described in its README, with a
`config.json` that enables the app and describes the house:

```
"apps": ["hearth"],
"homeassistant": {"url": "http://homeassistant.local:8123", "token": "..."},
"household": {"people": ["Alex", "Sam"], "pets": {"Bella": "cat", "Rex": "dog"}},
"infra_words": ["myserver", "rack"]
```

`python -m stonesage.first_run` asks for these. Hearth needs a Home Assistant instance with a long-lived access
token; Frigate, cameras and speakers are optional and switch features on when configured.

### Set up in the browser

Open the StoneSage cockpit, go to the Smart Home tab (F5) and use the Hearth setup panel (a banner offers it until the
setup is complete). It walks through four steps: the Home Assistant address and token (with a Test button), discovery of
the people, phones and speakers Home Assistant knows (pets you add yourself), the cameras to show, and Save. Saving keeps
every other setting in `config.json` and leaves a timestamped backup next to it. The token is stored only in
`config.json`; it is never shown again, returned by the API or logged. The command line does the same job:
`python -m stonesage.first_run`.

### Voice server vocabulary

The voice server corrects near-miss spellings of your household's names (a recogniser hears "Bela" for
"Bella"). The names live in a file that is not tracked: copy `deploy/voice-server/vocab.example.txt` to
`deploy/voice-server/vocab.txt` (or point `VOICE_VOCAB` at your own file) and put your own names in it. Without a
`vocab.txt` the server still starts and simply corrects nothing.

## Backups and upgrades

StoneSage writes a backup of `config.json`, the souls and the small state files every night (config `backup`), keeps the newest
14, and writes one before it migrates an old settings file; `python -m stonesage.backup create | list | restore <file>` does it
by hand. A backup holds your secrets and there is no download route: copy the files off the machine yourself, or let StoneSage
do it after every backup (config `backup.destinations`: a NAS or USB folder, a synced Google Drive / Dropbox folder, or any
`rclone` remote, which must be encrypted unless you say otherwise). Upgrading is
"get the new version and restart" (`docker compose pull && docker compose up -d`, or `git pull`); a settings file written by a
newer version is refused. [docs/UPGRADING.md](docs/UPGRADING.md) covers upgrading, rolling back and moving to another machine.

## Tests

```
python tests/run_tests.py
```

The runner is offline by design: it refuses every connection that is not to localhost, and refuses to run network
programs such as `ssh`, so tests never reach a real house. The Alexa skill tests run the skill's JavaScript and need
Node.js (CI uses 20).

## Licence

[The Unlicense](LICENSE): public domain, do what you like.
