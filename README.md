# Chat Clipper

Automatically find the best moments in long streams (YouTube, Twitch, Kick) by detecting **chat spikes**, starting each cut **20 seconds early**, and exporting **vertical 9:16 clips** ready for captions.

## How It Works

Instead of using expensive AI video analysis or watching a 6-hour stream yourself, this tool uses **live chat as a highlight radar**:

1. **Fetch Chat First:** Downloads the stream's timestamped chat log (`chat-downloader` or `yt-dlp` `live_chat.json`) without downloading the full video first.
2. **10-Second Bucketing & Hype Scoring:** Groups chat messages into 10-second windows. Each message adds `1.0` to its bucket, plus a `+0.5` bonus if it contains hype words (`LMAO`, `KEKW`, `OMG`, `WTF`, `CLIP`, `W`, etc.).
3. **Spike Detection:** Finds windows where chat explodes above the stream's average (`mean + 2.0 * std_dev`), then removes overlapping spikes so you only get distinct moments.
4. **20-Second Early Cut:** Because chat reacts *after* something crazy happens, each clip starts **20 seconds before** the spike (`--pre-spike 20`) and runs for 45 seconds (`--duration 45`).
5. **Fast Section Download + 9:16 Vertical Export:** Downloads only the winning 45-second segments via `yt-dlp --download-sections` (with automatic full-VOD fallback if restricted) and converts them to `1080×1920` vertical `.mp4` files using `ffmpeg` (blurred background by default, or center-crop).
6. **Live Mode (`--live`):** Monitors live (or scheduled) streams in real time against a 3-minute rolling average and clips only when chat surges (`2.5×` above average by default), capped by `--top`.

## Installation (Windows)

```powershell
# 1. Install FFmpeg (and Node.js if you don't have it, for YouTube downloads)
winget install Gyan.FFmpeg

# 2. Clone this repo & install Python dependencies
git clone https://github.com/YOUR_USERNAME/chat-clipper.git
cd chat-clipper
pip install -r requirements.txt
```

*(Restart PowerShell after installing FFmpeg so it's on your `PATH`.)*

## Usage

### 1. Clip a Finished Stream VOD (Recommended)
```powershell
python clipper.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --top 5 --out my_clips
```

### 2. Monitor a Live / Scheduled Stream (`--live`)
```powershell
python clipper.py --url "https://www.youtube.com/watch?v=LIVE_OR_SCHEDULED_ID" --live --top 5 --out live_clips
```
*(Requires YouTube DVR to be enabled on the stream.)*

### 3. Use Local Files (Video + Chat JSON)
Works with JSON from `chat-downloader`, `yt-dlp` (`.live_chat.json`), or `TwitchDownloader`:
```powershell
python clipper.py --video vod.mp4 --chat chat.json --out my_clips
```

### 4. Preview Timestamps Only (`--dry-run`)
```powershell
python clipper.py --video vod.mp4 --chat chat.json --dry-run
```

## Useful Options

- `--top 10`: Max number of clips to export (default: `10`).
- `--pre-spike 20`: Seconds to start *before* the chat spike (default: `20`).
- `--duration 45`: Length of each clip in seconds (default: `45`).
- `--layout blur`: Keeps full `16:9` gameplay with blurred top/bottom (default). Use `--layout crop` for center-cropped `9:16`.
- `--std 2.0`: Spike sensitivity for VODs (lower = more clips, higher = only the biggest moments).
- `--live-ratio 2.5`: In `--live` mode, chat must jump at least this many times the 3-minute rolling average (default: `2.5`).
- `--min-live-msgs 40`: Minimum weighted score in 10s to trigger a live clip (default: `40`).

## Final Step: Add Captions

Drop the exported `my_clips/` folder into **Open Shorts**, **CapCut**, or **DaVinci Resolve** to add auto-captions and export for Reels, TikTok, or YouTube Shorts.
