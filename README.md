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

## Road image score API (OpenRouter)

Scores a **street/road photo** from **1** (best — good to use) to **100** (worst). Uses a **vision** model on [OpenRouter](https://openrouter.ai/) (OpenAI-compatible API).

**Security:** keep `OPENROUTER_API_KEY` only in `.env` (never commit). If a key was ever pasted into chat or git, **revoke it** on OpenRouter and create a new one.

```bash
cp .env.example .env
# Edit .env: set OPENROUTER_API_KEY and optionally OPENROUTER_MODEL

pip install -r api/requirements.txt
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

- **OpenAPI (live):** http://127.0.0.1:8000/docs — interactive Swagger UI.  
- **OpenAPI JSON:** http://127.0.0.1:8000/openapi.json  
- **Static template for frontends:** [`openapi/road-score.yaml`](openapi/road-score.yaml) (import into Postman, Orval, etc.)

**Endpoint:** `POST /v1/road-score` — `multipart/form-data` field `image` (file). Optional query `model=…` to override the OpenRouter model id.

**Response:** `score` (1–100), `rationale` (one line), `explanation` (paragraph), `analysis` (`pavement_surface`, `visibility_environment`, `hazards_constraints`, `scene_context`), and `model`.

## Speech-to-text (infinite stream → file)

Runs until you press **Ctrl+C**. Decodes the HLS URL to 16 kHz mono PCM, transcribes each chunk with **local Whisper** ([faster-whisper](https://github.com/SYSTRAN/faster-whisper)), **appends** lines to a text file.

```bash
pip install -e ".[stt]"    # pulls faster-whisper (downloads model on first run)

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

**Background (macOS / Linux):**

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

## Note

Stream metadata via **ffprobe** depends on what the broadcaster puts in the container; many AAC HLS feeds expose **few or no** tags. This project only **consumes** the HTTP(S) stream — no RF hardware.

See **`requirements.txt`** for the editable install line.

### Pip: `Skipping … rds_extract.egg-info due to invalid metadata`

That folder is leftover from an old package name. Remove it, then reinstall:

```bash
rm -rf rds_extract.egg-info
pip install -e .
```
