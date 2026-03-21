# traffic-simulation

Consume **HLS** (`.m3u8`) internet radio — default: [VOH channel 5](https://strm.voh.com.vn/radio/channel5/playlist.m3u8).

## Requirements

- **Python** 3.9+
- **`run.py`**: no system ffmpeg — use `pip install -r requirements.txt` (`imageio-ffmpeg`).
- **`voh-hls` / play / record**: system **ffmpeg** on `PATH` (`brew install ffmpeg`); optional **mpv** for playback.

## Setup

```bash
pip install -e .
```

## How to run the Road score API (FastAPI)

Scores a **street/road photo** from **1** (best — good to use) to **100** (worst) using a **vision** model on [OpenRouter](https://openrouter.ai/) (OpenAI-compatible API).

**Security:** keep `OPENROUTER_API_KEY` only in `.env` (never commit). If a key was ever pasted into chat or git, **revoke it** on OpenRouter and create a new one.

The **app** is `api.main:app`, served with **Uvicorn**. Use a **foreground** terminal while developing; use **background** when you want it to keep running after you disconnect.

**One-time env (from repo root):**

```bash
cp .env.example .env
# Edit .env: OPENROUTER_API_KEY, optionally OPENROUTER_MODEL

pip install -r api/requirements.txt
# faster-whisper is included: first audio transcribe downloads Whisper weights (see WHISPER_MODEL_SIZE).
```

**Venv:** create **`python3 -m venv .venv`** (or **`venv`**) at the repo root. **`script/crawl.sh`** uses that interpreter and, if **`httpx`** is missing, runs **`pip install -r requirements-crawl.txt`** once (disable with **`CRAWL_NO_BOOTSTRAP=1`**). For the API, install **`pip install -r api/requirements.txt`**. **`script/host-api.sh`** also prefers **`.venv/bin/python3`** / **`venv/bin/python3`**.

### Foreground (attached terminal)

Stop with **Ctrl+C**.

- **Development** — auto-reload on code changes:

  ```bash
  uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
  ```

  Or from repo root: **`./script/host-api.sh`** (optional **`PORT=8080 ./script/host-api.sh`**; extra args are passed to uvicorn). **Detached:** **`./script/host-api.sh --background`** (or **`API_BACKGROUND=1`**); logs **`api.log`**, PID **`api.pid`** — no **`--reload`** (pass **`--reload`** after **`--background`** if you really want it).

- **Without reload** (closer to production, single process):

  ```bash
  uvicorn api.main:app --host 0.0.0.0 --port 8000
  ```

### Background (detached)

Do **not** use **`--reload`** in the background default (file watching is a poor fit for `nohup`). Prefer the helper:

```bash
./script/host-api.sh --background
# or: API_BACKGROUND=1 ./script/host-api.sh
tail -f api.log
```

Equivalent manual `nohup`:

```bash
nohup uvicorn api.main:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &
echo $! > api.pid
```

Stop API: **`kill "$(cat api.pid)"`** and **`rm -f api.pid`** (from repo root).

**Media crawl in background** (loop + logs **`crawl.log`**, PID **`crawl.pid`**):

```bash
./script/crawl.sh --background
# one-shot cron-style: ./script/crawl.sh -b --once
```

Stop crawl: **`kill "$(cat crawl.pid)"`**.

`api.log`, **`crawl.log`**, and `*.pid` are gitignored.

### Where it listens

- **OpenAPI (live):** http://127.0.0.1:8000/docs — interactive Swagger UI.  
- **OpenAPI JSON:** http://127.0.0.1:8000/openapi.json  
- **Static template for frontends:** [`openapi/road-score.yaml`](openapi/road-score.yaml) and JSON twin [`openapi/openapi.json`](openapi/openapi.json) (same paths/schemas: `/health`, `/v1/road-score`, `/v1/media-seed/latest`).  
- **Full machine-generated spec** (all routes including `/v1/media/*`): http://127.0.0.1:8000/openapi.json — or `python scripts/export_openapi_json.py` → [`openapi/openapi-from-app.json`](openapi/openapi-from-app.json).

**Endpoint:** `POST /v1/road-score` — `multipart/form-data` field `image` (file). Optional query `model=…` to override the OpenRouter model id.

**Response:** `score` (1–100), `rationale` (one line), `explanation` (paragraph), `analysis` (`pavement_surface`, `visibility_environment`, `hazards_constraints`, `scene_context`), and `model`.

**Media (same 1–100 scale + explanation):**

| Method | Path | Body |
|--------|------|------|
| `POST` | `/v1/media/image-score` | `image` file — same road/traffic rubric as `/v1/road-score` |
| `POST` | `/v1/media/audio-score` | `audio` file — **Whisper** (faster-whisper) transcription then **score the transcript** (default). Env **`AUDIO_TRANSCRIBE_BACKEND=openrouter`** uses OpenRouter `input_audio` instead. `?direct=true` = legacy one-shot audio JSON. **`WHISPER_MODEL_SIZE`**, **`WHISPER_LANGUAGE`**, etc. in `.env`. |
| `POST` | `/v1/media/text-score` | JSON body `{"text":"…"}` — score the paragraph for traffic/mobility usefulness |
| `GET` | `/v1/media-seed/latest` | **`routes`**: **`route`**, **`score`**, **`reason`**. All **image** rows use **`route` = Ngã tư Phú Nhuận** (override **`MEDIA_SEED_IMAGE_ROUTE_NAME`**). Text/audio use model/heuristics. **`full=true`**, **`MEDIA_SEED_EXPORT_PATH`**. |

- Image model: **`OPENROUTER_MODEL`** (default `openai/gpt-4o-mini`).  
- Audio model: **`OPENROUTER_MEDIA_AUDIO_MODEL`** (default `google/gemini-2.0-flash-001` — use an [audio-input](https://openrouter.ai/docs/guides/overview/multimodal/audio) model if you change it).

From repo root run **`./script/crawl.sh`** — by default it **loops forever** (interval from **`CRAWL_INTERVAL_SECONDS`** in `scripts/media_seed_crawl.py`; set **`CRAWL_LOOP_FOREVER = False`** there for a single run without flags). **One crawl then exit:** **`./script/crawl.sh --once`**. **Background:** **`./script/crawl.sh --background`** or **`CRAWL_BACKGROUND=1`** → **`crawl.log`** + **`crawl.pid`**. Other flags, e.g. **`./script/crawl.sh -b --once -v`**, are passed through. Under the hood: `scripts/media_seed_crawl.py`.

The **Media Seed crawler** (`scripts/media_seed_crawl.py`) targets the app’s crawl hooks: **`data-crawl-content="text"`** (textarea / text → `crawl_text.txt`), **`data-crawl-content="image"`** (the `<img src="…">` inside it, e.g. Supabase storage URLs), and **`data-crawl-content="audio"`** (or **`section#audio`**). With **Playwright** (**`HARVEST_MEDIA_SEED_UI_PANELS`**), it also resolves **`blob:`** / **`data:`** previews. Plain **HTTP** fetch parses the same markers when they appear in HTML and downloads **https** assets only. With **`OPENROUTER_API_KEY`**, it scores text / image / audio via the same OpenRouter flow as `/v1/media/*-score`. Install: **`pip install -r requirements-crawl.txt`**, then **`python -m playwright install chromium`** (downloads the browser; **`./script/crawl.sh`** runs this for you unless **`CRAWL_NO_PLAYWRIGHT_INSTALL=1`**). For scoring: **`pip install -r api/requirements.txt`**. Flags: **`--no-download-media`**, **`--no-openrouter`**. Set **`HARVEST_MEDIA_SEED_UI_PANELS = False`** to fall back to the older “scan all HTML for media URLs” behavior (still not a screenshot).

**Export shape:** besides **`_meta.media_downloads`**, each successful OpenRouter score is copied to the **root** of the JSON as `"<key>": "<score>"` (e.g. first line of paragraph text, `image:filename.jfif`, `audio:file.mp3`). **`_meta.score_keys_this_run`** lists all those keys for this crawl; **`_meta.openrouter_media_score_keys`** lists only keys that came from scored media.

## Speech-to-text (infinite stream → file)

Decodes the HLS URL to 16 kHz mono PCM, transcribes each chunk with **local Whisper** ([faster-whisper](https://github.com/SYSTRAN/faster-whisper)), and **appends** lines to a text file.

```bash
pip install -e ".[stt]"    # pulls faster-whisper (downloads model on first run)
```

### Foreground

Runs in your terminal until **Ctrl+C**.

```bash
python transcribe_stream.py -o transcript.txt
# or
voh-transcribe -o transcript.txt

# Options
python transcribe_stream.py -o out.txt --model small --chunk-sec 10 --language vi
python transcribe_stream.py -o out.txt --url 'https://strm.voh.com.vn/radio/channel5/playlist.m3u8'
```

- **CPU**: use `--model tiny` or `base` if chunks fall behind real time.
- **GPU**: `--device cuda` (and suitable `--compute-type float16`).
- **Language**: default `vi`; use `--language ''` for auto-detect (slower).

### Background (macOS / Linux)

Same process, but **`nohup`** + PID file so it survives closing the terminal.

```bash
./transcribe_bg.sh -o transcript.txt
tail -f transcribe.log          # ffmpeg / python stderr
./transcribe_bg.sh status
./transcribe_bg.sh stop
```

Logs go to `transcribe.log` by default; override with `TRANSCRIBE_LOG=/path/to.log`.

## One script (print stream info as text)

```bash
pip install -r requirements.txt   # pulls imageio-ffmpeg (bundled ffmpeg — no Homebrew)
python3 run.py
```

Optional URL: `python3 run.py 'https://…/playlist.m3u8'`. Uses the **pip-installed** ffmpeg binary (`imageio-ffmpeg`), else raw **HTTPS** `.m3u8` text.

## CLI

From the repo root (or after install):

```bash
./stream_voh.sh metadata
./stream_voh.sh play
./stream_voh.sh record -o voh.aac -t 300
./stream_voh.sh watch-metadata

python3 -m radio_hls metadata --url 'https://strm.voh.com.vn/radio/channel5/playlist.m3u8'
voh-hls play
```

Use **`--url`** for other playlists on the same host (e.g. other `channelN` paths).

## Python

```python
from radio_hls.hls_stream import ffprobe_json, DEFAULT_VOH_HLS

data = ffprobe_json(DEFAULT_VOH_HLS)
print(data["format"].get("tags", {}))
```

## Media Seed Bot crawler (Lovable app → JSON)

Crawls **[media-seed-bot.lovable.app](https://media-seed-bot.lovable.app/)**, **logs the extracted page text** (stderr or a log file you redirect), and writes **`media_seed_export.json`**: place-like labels mapped to string scores (e.g. `"ngã 4 phú nhuận": "42"`), plus **`_meta`** (crawl time, URL, score keys) and **`_meta.raw`**: fetch mode, HTTP status / final URL / content-type (HTTP mode), **full `extracted_text`**, **`html_preview`** (truncated raw HTML, length in `html_length`), **`html_truncated`**, and **`json_blobs_count`**.

The public page is mostly a **client-side** upload UI; if scores or listings appear only after JavaScript runs, use **`--browser`** (Playwright).

```bash
pip install -r requirements-crawl.txt
python -m playwright install chromium   # browser binary; required for Playwright crawl (script/crawl.sh runs this too)
```

Default **interval** and **loop vs single-run** are set in **`scripts/media_seed_crawl.py`** at the top **`CONFIG`** block (`CRAWL_LOOP_FOREVER`, `CRAWL_INTERVAL_SECONDS`, etc.). CLI flags override those values.

### Foreground

**Default (from CONFIG)** — usually loops every **60 seconds** until **Ctrl+C** (overwrites the JSON each run with the latest crawl + raw block):

```bash
python scripts/media_seed_crawl.py -o media_seed_export.json
```

**Other intervals** — `--interval SEC` (still loops until Ctrl+C).

**Single run** — for manual checks or **cron** (one process per schedule line):

```bash
python scripts/media_seed_crawl.py -o media_seed_export.json --once
```

### Background

**Cron** (every minute, **one shot** per invocation — use **`--once`** so the job doesn’t stay alive for 60s loops inside one cron tick):

```bash
* * * * * cd /path/to/traffic-simulation && .venv/bin/python scripts/media_seed_crawl.py --once >> logs/media_seed.log 2>&1
```

**`nohup`** (same as foreground default: **60s loop** until you kill it):

```bash
mkdir -p logs
nohup python scripts/media_seed_crawl.py -o media_seed_export.json >> logs/media_seed.log 2>&1 &
```

Environment: **`MEDIA_SEED_URL`** overrides the default URL. Flags: **`--browser`**, **`--replace`**, **`--interval`**, **`--once`**, **`--max-raw-html N`** (cap for `html_preview`, default 262144), **`-v`**.

## Note

Stream metadata via **ffprobe** depends on what the broadcaster puts in the container; many AAC HLS feeds expose **few or no** tags. This project only **consumes** the HTTP(S) stream — no RF hardware.

See **`requirements.txt`** for the editable install line.

### Pip: `Skipping … rds_extract.egg-info due to invalid metadata`

That folder is leftover from an old package name. Remove it, then reinstall:

```bash
rm -rf rds_extract.egg-info
pip install -e .
```
