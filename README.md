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
| 2 | **Hearth** (this repository) | Apps, starting with a Home Assistant house assistant |
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

## Install with Docker

Docker works the same on Linux, Windows and macOS. Hearth's dependencies include Lodestone, whose installer writes the
Compose files for the whole stack: language models, a gateway in front of them, StoneSage with Hearth, and a model
download on the first start.

```
pip install git+https://github.com/lottage/hearth       # brings Lodestone and StoneSage along
python -m lodestone.installer --docker                  # looks at the machine, writes docker-compose.yml and .env here
# fill HASS_TOKEN (a Home Assistant long-lived access token) and LODESTONE_API_KEY in .env, then
docker compose up -d
```

The cockpit is on `http://localhost:8888` and the models' OpenAI-compatible address on `http://localhost:8000/v1`, both
bound to this computer only until you change `STONESAGE_BIND` / `LODESTONE_BIND` in `.env`. On the first start
`config.json` is written into the `stonesage-config` volume from the environment (Hearth enabled, the engines as
`cluster.*_url`, your Home Assistant URL and token); after that it is yours to edit in the cockpit. Describe the house
there (`household`, `infra_words`, see below). Without Python, `docker/docker-compose.yml` and `docker/.env.example` in
this repository are a ready CPU stack.

| Docker on | CPU | NVIDIA GPU | AMD or Intel GPU |
| --- | --- | --- | --- |
| Linux (x86-64 or arm64) | yes | yes, with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) | yes, through `/dev/dri` (Vulkan) |
| Windows (Docker Desktop, WSL2) | yes | yes, with a current NVIDIA driver | no |
| macOS (Docker Desktop, Intel or Apple Silicon) | yes | no | no |

CPU is the default when no usable GPU is visible, so the same command works everywhere (give Docker Desktop at least 8 GB
of memory for it). `--gpu vulkan|cuda|cpu`, `--profile gpu-large|gpu-small|cpu`, `--vision` and `--out DIR` adjust the
plan. The Hearth image is the StoneSage image plus this app (`Dockerfile` here, `FROM` the StoneSage image). This path is
tested offline only; it has not yet been run on real GPUs. Until the images are published to ghcr.io, build them
from the Dockerfiles (each one's header says how: Lodestone's, then StoneSage's, then this one).

## Install without Docker (bare metal)

Hearth depends on Lodestone and StoneSage (neither is on PyPI); pip fetches both from GitHub as part of the install:

```
git clone https://github.com/lottage/hearth
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

### Voice server vocabulary

The voice server corrects near-miss spellings of your household's names (a recogniser hears "Bela" for
"Bella"). The names live in a file that is not tracked: copy `deploy/voice-server/vocab.example.txt` to
`deploy/voice-server/vocab.txt` (or point `VOICE_VOCAB` at your own file) and put your own names in it. Without a
`vocab.txt` the server still starts and simply corrects nothing.

## Tests

```
python tests/run_tests.py
```

The runner is offline by design: it refuses every connection that is not to localhost, and refuses to run network
programs such as `ssh`, so tests never reach a real house. The Alexa skill tests run the skill's JavaScript and need
Node.js (CI uses 20).

## Licence

[The Unlicense](LICENSE): public domain, do what you like.
