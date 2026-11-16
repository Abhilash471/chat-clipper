"""Chat-spike auto-clipper for long-form streams (YouTube, Twitch, Kick).

Finds moments where stream chat explodes, starts the cut 20 seconds early
(because chat reacts after the moment), and exports vertical 9:16 clips
ready for captioning in Open Shorts, CapCut, or DaVinci Resolve.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_HYPE_WORDS = (
    "lmao",
    "lmfao",
    "kekw",
    "lul",
    "omegalul",
    "omg",
    "wtf",
    "clip",
    "clips",
    "w",
    "holy",
    "crazy",
    "insane",
    "no way",
    "bro",
)


@dataclass(frozen=True)
class ChatMessage:
    """A single chat message with a timestamp relative to stream start."""

    offset_seconds: float
    text: str


@dataclass(frozen=True)
class ClipWindow:
    """A detected highlight window to cut from the source video."""

    start_seconds: float
    end_seconds: float
    spike_seconds: float
    score: float
    message_count: int


def parse_chat_json(chat_path: Path) -> list[ChatMessage]:
    """Load chat messages from yt-dlp, chat-downloader, or TwitchDownloader JSON."""
    raw_text = chat_path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw_text:
        return []

    # yt-dlp writes NDJSON (.live_chat.json); other tools write a single JSON value.
    if raw_text.startswith("{") and "\n" in raw_text:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return _parse_ytdlp_ndjson(raw_text.splitlines())
    else:
        data = json.loads(raw_text)

    if isinstance(data, list):
        return _parse_chat_downloader_list(data)
    if isinstance(data, dict):
        if "comments" in data and isinstance(data["comments"], list):
            return _parse_twitch_downloader(data["comments"])
        return _parse_ytdlp_ndjson([raw_text])

    raise ValueError(f"Unsupported chat JSON format in {chat_path}")


def _parse_chat_downloader_list(items: list[dict]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for item in items:
        ts = item.get("time_in_seconds")
        if ts is None:
            continue
        ts_val = float(ts)
        if ts_val < 0:
            continue
        messages.append(ChatMessage(ts_val, str(item.get("message", ""))))
    return messages


def _parse_twitch_downloader(comments: list[dict]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for item in comments:
        ts = item.get("content_offset_seconds")
        if ts is None:
            continue
        ts_val = float(ts)
        if ts_val < 0:
            continue
        body = ""
        msg_obj = item.get("message")
        if isinstance(msg_obj, dict):
            body = str(msg_obj.get("body", ""))
        messages.append(ChatMessage(ts_val, body))
    return messages


def _parse_ytdlp_ndjson(lines: Iterable[str]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        action = entry.get("replayChatItemAction")
        if not isinstance(action, dict):
            continue
        offset_msec = action.get("videoOffsetTimeMsec")
        if offset_msec is None:
            continue
        ts_val = float(offset_msec) / 1000.0
        if ts_val < 0:
            continue

        text = _extract_ytdlp_text(action.get("actions", []))
        messages.append(ChatMessage(ts_val, text))
    return messages


def _extract_ytdlp_text(actions: list[dict]) -> str:
    parts: list[str] = []
    for act in actions:
        item = act.get("addChatItemAction", {}).get("item", {})
        renderer = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not isinstance(renderer, dict):
            continue
        for run in renderer.get("message", {}).get("runs", []):
            if "text" in run:
                parts.append(str(run["text"]))
    return "".join(parts)


def _message_score(text: str, hype_weight: float = 0.5) -> float:
    lower = text.lower()
    bonus = hype_weight if any(word in lower for word in DEFAULT_HYPE_WORDS) else 0.0
    return 1.0 + bonus


def find_chat_spikes(
    messages: list[ChatMessage],
    bucket_seconds: int = 10,
    pre_spike_seconds: int = 20,
    clip_duration: int = 45,
    top_n: int = 10,
    min_gap_seconds: int = 45,
    std_multiplier: float = 2.0,
    hype_weight: float = 0.5,
) -> list[ClipWindow]:
    """Bucket messages into fixed windows and return the top non-overlapping spikes."""
    if not messages:
        return []

    max_time = max(m.offset_seconds for m in messages)
    num_buckets = int(max_time // bucket_seconds) + 1
    counts = [0] * num_buckets
    scores = [0.0] * num_buckets

    for msg in messages:
        idx = int(msg.offset_seconds // bucket_seconds)
        counts[idx] += 1
        scores[idx] += _message_score(msg.text, hype_weight)

    mean_score = sum(scores) / len(scores)
    variance = sum((s - mean_score) ** 2 for s in scores) / len(scores)
    std_dev = math.sqrt(variance)
    threshold = mean_score + std_multiplier * std_dev

    # Rank candidate buckets from highest score to lowest.
    ranked = sorted(
        (
            (idx, scores[idx], counts[idx])
            for idx in range(num_buckets)
            if scores[idx] >= threshold and counts[idx] > 0
        ),
        key=lambda item: item[1],
        reverse=True,
    )

    selected: list[ClipWindow] = []
    for idx, score, count in ranked:
        spike_time = idx * bucket_seconds
        if any(abs(spike_time - existing.spike_seconds) < min_gap_seconds for existing in selected):
            continue

        start = max(0.0, spike_time - pre_spike_seconds)
        end = start + clip_duration
        selected.append(
            ClipWindow(
                start_seconds=start,
                end_seconds=end,
                spike_seconds=spike_time,
                score=round(score, 2),
                message_count=count,
            )
        )
        if len(selected) >= top_n:
            break

    return sorted(selected, key=lambda c: c.start_seconds)


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH-MM-SS for clean filenames."""
    total = int(seconds)
    hrs, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hrs:02d}-{mins:02d}-{secs:02d}"


def get_video_dimensions(video_path: Path) -> tuple[int, int] | None:
    """Return (width, height) of the primary video stream using ffprobe, or None."""
    if shutil.which("ffprobe") is None:
        return None
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split("x")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]), int(parts[1])
    return None


def cut_vertical_clip(
    video_path: Path,
    output_path: Path,
    window: ClipWindow,
    layout: str = "blur",
) -> None:
    """Cut a segment with ffmpeg and convert it to 1080x1920 (9:16 vertical)."""
    duration = window.end_seconds - window.start_seconds
    dims = get_video_dimensions(video_path)
    already_vertical = dims is not None and dims[1] > dims[0]

    if already_vertical:
        vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    elif layout == "crop":
        vf = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"
    elif layout == "blur":
        vf = (
            "split[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:5[bg_blur];"
            "[fg]scale=1080:-2[fg_scaled];"
            "[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2"
        )
    else:
        raise ValueError(f"Unknown layout '{layout}'. Expected 'blur' or 'crop'.")

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{window.start_seconds:.2f}",
        "-i",
        str(video_path),
        "-t",
        f"{duration:.2f}",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


def require_binary(name: str) -> None:
    """Exit with a helpful message if a required CLI binary is missing."""
    if shutil.which(name) is None:
        print(
            f"Error: '{name}' was not found on PATH. "
            f"Install it first (see README.md).",
            file=sys.stderr,
        )
        sys.exit(1)


def _ytdlp_base_cmd() -> list[str]:
    cmd = ["yt-dlp"]
    if shutil.which("node"):
        cmd.extend(["--js-runtimes", "node", "--remote-components", "ejs:github"])
    return cmd


def download_chat_only(url: str, work_dir: Path) -> Path:
    """Download only the chat log for a stream URL (chat_downloader or yt-dlp)."""
    work_dir.mkdir(parents=True, exist_ok=True)

    # Reuse cached chat if already downloaded in work_dir.
    for cached in [work_dir / "chat.json", *sorted(work_dir.glob("*.live_chat.json"))]:
        if cached.exists() and cached.stat().st_size > 2:
            print(f"Using cached chat: {cached}")
            return cached

    require_binary("yt-dlp")
    chat_path = work_dir / "chat.json"
    if shutil.which("chat_downloader"):
        print("Downloading chat via chat_downloader...")
        res = subprocess.run(
            ["chat_downloader", url, "--output", str(chat_path)],
        )
        if res.returncode == 0 and chat_path.exists() and chat_path.stat().st_size > 2:
            return chat_path

    print("Trying yt-dlp live_chat subtitles...")
    subprocess.run(
        [
            *_ytdlp_base_cmd(),
            "--skip-download",
            "--write-subs",
            "--sub-langs",
            "live_chat",
            "-o",
            str(work_dir / "vod"),
            url,
        ],
        check=False,
    )
    live_chats = sorted(work_dir.glob("*.live_chat.json"))
    if live_chats:
        return live_chats[0]

    raise FileNotFoundError(
        "No chat replay found for this URL. Make sure the URL is a finished stream VOD "
        "with chat replay enabled (not a regular video or active live stream)."
    )


def download_vod_section(
    url: str,
    window: ClipWindow,
    dest_path: Path,
    live_from_start: bool = False,
) -> Path:
    """Download only a specific time range from a stream URL using yt-dlp."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    section = f"*{int(window.start_seconds)}-{int(window.end_seconds)}"
    template = str(dest_path.with_suffix(".%(ext)s"))
    cmd = [*_ytdlp_base_cmd()]
    if live_from_start:
        cmd.append("--live-from-start")
    cmd.extend([
        "--download-sections",
        section,
        "-f",
        "bv*[height<=1080]+ba/bv*[width<=1080]+ba/b",
        "--merge-output-format",
        "mp4",
        "-o",
        template,
        url,
    ])
    subprocess.run(cmd, check=True)
    candidates = sorted(dest_path.parent.glob(f"{dest_path.stem}.*"))
    video_path = next((p for p in candidates if p.suffix in {".mp4", ".mkv", ".webm"}), None)
    if video_path is None:
        raise FileNotFoundError(f"Could not find downloaded section in {dest_path.parent}")
    return video_path


def download_full_vod(url: str, work_dir: Path) -> Path:
    """Download the full stream VOD using yt-dlp (reuses existing vod.* if present)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    existing = next(
        (p for p in sorted(work_dir.glob("vod.*")) if p.suffix in {".mp4", ".mkv", ".webm"}),
        None,
    )
    if existing is not None:
        print(f"Using cached video: {existing}")
        return existing

    video_template = str(work_dir / "vod.%(ext)s")
    print(f"Downloading full video from {url}...")
    subprocess.run(
        [
            *_ytdlp_base_cmd(),
            "-f",
            "bv*[height<=1080]+ba/bv*[width<=1080]+ba/b",
            "--merge-output-format",
            "mp4",
            "-o",
            video_template,
            url,
        ],
        check=True,
    )
    videos = sorted(work_dir.glob("vod.*"))
    video_path = next((p for p in videos if p.suffix in {".mp4", ".mkv", ".webm"}), None)
    if video_path is None:
        raise FileNotFoundError(f"Could not find downloaded video in {work_dir}")
    return video_path


def _export_live_clip(
    url: str,
    window: ClipWindow,
    clip_index: int,
    out_dir: Path,
    work_dir: Path,
    layout: str,
) -> None:
    """Download a live DVR section and export it as a 9:16 vertical clip."""
    out_file = out_dir / f"live_clip_{clip_index:02d}_{format_timestamp(window.start_seconds)}.mp4"
    raw_section = work_dir / f"live_section_{clip_index:02d}.mp4"
    print(f"\n[LIVE] Exporting {out_file.name} ({format_timestamp(window.start_seconds)} -> {format_timestamp(window.end_seconds)})...")
    try:
        section_video = download_vod_section(url, window, raw_section, live_from_start=True)
        full_window = ClipWindow(
            0.0, window.end_seconds - window.start_seconds, window.spike_seconds, window.score, window.message_count
        )
        cut_vertical_clip(section_video, out_file, full_window, layout=layout)
        print(f"[LIVE] Saved {out_file}")
    except Exception as exc:
        print(f"[LIVE] Could not export {out_file.name} (DVR may be unavailable): {exc}", file=sys.stderr)


def run_live_clipper(
    url: str,
    out_dir: Path,
    bucket_seconds: int = 10,
    pre_spike_seconds: int = 20,
    clip_duration: int = 45,
    top_n: int = 10,
    min_gap_seconds: int = 120,
    std_multiplier: float = 2.5,
    live_ratio: float = 2.5,
    min_live_msgs: int = 40,
    layout: str = "blur",
    dry_run: bool = False,
) -> None:
    """Monitor a live or scheduled stream in real time and clip chat spikes."""
    try:
        from chat_downloader import ChatDownloader
    except ImportError:
        print("Error: 'chat-downloader' Python package is required for --live mode.", file=sys.stderr)
        sys.exit(1)

    if not dry_run:
        require_binary("yt-dlp")
        require_binary("ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "_source"
    history: deque[float] = deque(maxlen=18)  # 3-minute rolling baseline at 10s buckets
    current_bucket_idx: int | None = None
    bucket_score = 0.0
    bucket_count = 0
    last_trigger_time = -float(min_gap_seconds)
    clip_index = 0
    stream_start_wall = time.monotonic()

    print(f"Starting live chat monitor for: {url}")
    print("Waiting for stream / chat messages (press Ctrl+C to stop)...")

    with ThreadPoolExecutor(max_workers=1) as executor:
        while True:
            try:
                chat = ChatDownloader().get_chat(url)
                for msg in chat:
                    ts_raw = msg.get("time_in_seconds")
                    ts = float(ts_raw) if ts_raw is not None else (time.monotonic() - stream_start_wall)
                    if ts < 0:
                        continue  # Ignore pre-stream waiting room chat

                    b_idx = int(ts // bucket_seconds)
                    if current_bucket_idx is None:
                        current_bucket_idx = b_idx

                    while b_idx > current_bucket_idx:
                        if len(history) >= 6:
                            mean_score = sum(history) / len(history)
                            std_dev = math.sqrt(sum((s - mean_score) ** 2 for s in history) / len(history))
                            threshold = max(
                                float(min_live_msgs),
                                mean_score * live_ratio,
                                mean_score + std_multiplier * std_dev,
                            )
                            spike_time = current_bucket_idx * bucket_seconds
                            if bucket_score >= threshold and (spike_time - last_trigger_time) >= min_gap_seconds:
                                clip_index += 1
                                last_trigger_time = spike_time
                                start = max(0.0, spike_time - pre_spike_seconds)
                                window = ClipWindow(
                                    start_seconds=start,
                                    end_seconds=start + clip_duration,
                                    spike_seconds=spike_time,
                                    score=round(bucket_score, 2),
                                    message_count=bucket_count,
                                )
                                print(
                                    f"\n[SPIKE #{clip_index}/{top_n}] @ {format_timestamp(spike_time)} "
                                    f"({bucket_count} msgs, score {window.score} >= {threshold:.1f})"
                                )
                                if not dry_run:
                                    executor.submit(
                                        _export_live_clip, url, window, clip_index, out_dir, work_dir, layout
                                    )
                                if clip_index >= top_n:
                                    print(f"\nReached max clip limit (--top {top_n}). Stopping live monitor.")
                                    return
                        history.append(bucket_score)
                        bucket_score = 0.0
                        bucket_count = 0
                        current_bucket_idx += 1

                    bucket_count += 1
                    bucket_score += _message_score(str(msg.get("message", "")))

                print("Chat stream ended.")
                break
            except KeyboardInterrupt:
                print("\nStopping live monitor...")
                break
            except Exception as exc:
                print(f"Waiting for stream to go live ({exc}). Retrying in 30s...")
                try:
                    time.sleep(30)
                except KeyboardInterrupt:
                    print("\nStopping live monitor...")
                    break


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find chat spikes in a stream VOD and export vertical 9:16 clips."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Stream VOD URL (YouTube, Twitch, Kick)")
    source.add_argument("--video", type=Path, help="Path to local VOD video file")

    parser.add_argument(
        "--chat",
        type=Path,
        help="Path to chat JSON file (required when using --video)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("clips"),
        help="Output folder for vertical clips (default: ./clips)",
    )
    parser.add_argument("--bucket", type=int, default=10, help="Bucket size in seconds (default: 10)")
    parser.add_argument(
        "--pre-spike",
        type=int,
        default=20,
        help="Seconds to start before the chat spike (default: 20)",
    )
    parser.add_argument("--duration", type=int, default=45, help="Clip length in seconds (default: 45)")
    parser.add_argument("--top", type=int, default=10, help="Max number of clips to export (default: 10)")
    parser.add_argument(
        "--min-gap",
        type=int,
        default=45,
        help="Minimum seconds between detected spikes (default: 45)",
    )
    parser.add_argument(
        "--std",
        type=float,
        default=2.0,
        help="Spike threshold in standard deviations above mean (default: 2.0)",
    )
    parser.add_argument(
        "--layout",
        choices=("blur", "crop"),
        default="blur",
        help="Vertical 9:16 style: 'blur' keeps full 16:9 frame with blurred top/bottom; 'crop' center-crops",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Monitor a live (or scheduled) stream in real time and clip chat spikes as they happen",
    )
    parser.add_argument(
        "--min-live-msgs",
        type=int,
        default=40,
        help="Minimum weighted score in a 10s bucket to trigger a live clip (default: 40)",
    )
    parser.add_argument(
        "--live-ratio",
        type=float,
        default=2.5,
        help="In --live mode, chat must be at least this many times the 3-min average (default: 2.5)",
    )
    parser.add_argument(
        "--full-vod",
        action="store_true",
        help="When using --url, download the entire VOD instead of only the clip sections",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print detected timestamps without running ffmpeg",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.video and not args.chat:
        parser.error("--chat is required when using --video")

    if args.live:
        if not args.url:
            parser.error("--url is required when using --live")
        run_live_clipper(
            url=args.url,
            out_dir=args.out,
            bucket_seconds=args.bucket,
            pre_spike_seconds=args.pre_spike,
            clip_duration=args.duration,
            top_n=args.top,
            min_gap_seconds=max(args.min_gap, 120),
            std_multiplier=max(args.std, 2.5),
            live_ratio=args.live_ratio,
            min_live_msgs=args.min_live_msgs,
            layout=args.layout,
            dry_run=args.dry_run,
        )
        return

    work_dir = args.out / "_source"
    if args.url:
        chat_path = download_chat_only(args.url, work_dir)
    else:
        chat_path = args.chat

    if not args.dry_run:
        require_binary("ffmpeg")

    messages = parse_chat_json(chat_path)
    print(f"Loaded {len(messages)} chat messages from {chat_path}")

    windows = find_chat_spikes(
        messages=messages,
        bucket_seconds=args.bucket,
        pre_spike_seconds=args.pre_spike,
        clip_duration=args.duration,
        top_n=args.top,
        min_gap_seconds=args.min_gap,
        std_multiplier=args.std,
    )

    if not windows:
        print("No chat spikes found above threshold. Try lowering --std (e.g. --std 1.5).")
        return

    print(f"\nFound {len(windows)} clip(s):")
    for i, w in enumerate(windows, start=1):
        print(
            f"  {i:02d}. {format_timestamp(w.start_seconds)} -> {format_timestamp(w.end_seconds)} "
            f"(spike @ {format_timestamp(w.spike_seconds)}, {w.message_count} msgs, score {w.score})"
        )

    if args.dry_run:
        return

    args.out.mkdir(parents=True, exist_ok=True)
    if args.url and not args.full_vod:
        cached_vod: Path | None = None
        for i, w in enumerate(windows, start=1):
            out_file = args.out / f"clip_{i:02d}_{format_timestamp(w.start_seconds)}.mp4"
            if cached_vod is None:
                raw_section = work_dir / f"section_{i:02d}.mp4"
                print(f"\nDownloading & converting {out_file.name}...")
                try:
                    section_video = download_vod_section(args.url, w, raw_section)
                    full_window = ClipWindow(
                        0.0, w.end_seconds - w.start_seconds, w.spike_seconds, w.score, w.message_count
                    )
                    cut_vertical_clip(section_video, out_file, full_window, layout=args.layout)
                    continue
                except subprocess.CalledProcessError:
                    print("Section download failed (403/stream restriction); falling back to full VOD download...")
                    cached_vod = download_full_vod(args.url, work_dir)
            print(f"\nCutting {out_file.name}...")
            cut_vertical_clip(cached_vod, out_file, w, layout=args.layout)
    else:
        video_path = download_full_vod(args.url, work_dir) if args.url else args.video
        for i, w in enumerate(windows, start=1):
            out_file = args.out / f"clip_{i:02d}_{format_timestamp(w.start_seconds)}.mp4"
            print(f"\nCutting {out_file.name}...")
            cut_vertical_clip(video_path, out_file, w, layout=args.layout)

    print(f"\nDone! {len(windows)} vertical clips saved to: {args.out.resolve()}")
    print("Next step: drop this folder into Open Shorts, CapCut, or DaVinci Resolve for captions.")


if __name__ == "__main__":
    main()
