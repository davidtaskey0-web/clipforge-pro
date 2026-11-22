#!/usr/bin/env python3
"""
ClipForge Pro — Free, Unlimited OpusClip-Style AI Video Clipper
Scans the entire video automatically (Heatmap + Live Chat + Auto-Transcript)
to find, score, and render the best 9:16 clips for TikTok, Reels, Shorts & iPhone.
"""

import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", 8000))
HOST = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
OUT_DIR = Path("/tmp/clipforge_exports") if "PORT" in os.environ else (Path.cwd() / "clipforge_exports")
UPLOAD_DIR = Path("/tmp/clipforge_uploads") if "PORT" in os.environ else (Path.cwd() / "clipforge_uploads")
OUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

JOBS: dict[str, dict] = {}
ANALYZE_JOBS: dict[str, dict] = {}


def sec_to_hms(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = min(99, int(round((sec % 1) * 100)))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ytdlp_base_cmd(cookies_path: Path | None = None, browser: str | None = None) -> list[str]:
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-check-certificates",
        "--geo-bypass",
        "--no-playlist",
    ]
    if cookies_path and cookies_path.exists():
        cmd.extend(["--cookies", str(cookies_path)])
    elif browser:
        cmd.extend(["--cookies-from-browser", browser])
    return cmd


def get_browser_candidates() -> list[str | None]:
    # On local PC (no PORT env), try browser cookies automatically if default fails
    if "PORT" not in os.environ:
        return [None, "edge", "chrome", "firefox"]
    return [None]


def robust_ytdlp_download(url: str, start_hms: str, end_hms: str, out_mp4: Path, cookies_path: Path | None) -> None:
    """Try clean yt-dlp strategies + local browser cookies + pytubefix fallback."""
    last_err = "Could not download video"
    for browser in get_browser_candidates():
        for fmt in ["bv*+ba/b", "best[ext=mp4]/best"]:
            if out_mp4.exists():
                out_mp4.unlink()
            cmd = ytdlp_base_cmd(cookies_path, browser) + [
                "-f", fmt,
                "--merge-output-format", "mp4",
                "--download-sections", f"*{start_hms}-{end_hms}",
                "-o", str(out_mp4),
                url,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 1024:
                return
            if proc.stderr or proc.stdout:
                last_err = (proc.stderr or proc.stdout).strip().splitlines()[-1]

    # Fallback: pytubefix for YouTube URLs + ffmpeg trim
    if "youtube.com" in url or "youtu.be" in url:
        try:
            from pytubefix import YouTube
            yt = YouTube(url)
            stream = yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()
            if stream:
                stream.download(output_path=str(out_mp4.parent), filename="pytube_raw.mp4")
                raw_dl = out_mp4.parent / "pytube_raw.mp4"
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", start_hms, "-to", end_hms, "-i", str(raw_dl), "-c", "copy", str(out_mp4)],
                    check=True,
                )
                if out_mp4.exists() and out_mp4.stat().st_size > 1024:
                    return
        except Exception as exc:
            last_err = f"{last_err} | pytubefix: {exc}"

    raise RuntimeError(last_err)


DETECTORS = [
    ("Strong reaction", "Captures attention", re.compile(r"crazy|insane|what|omg|no way|lost|unreal|holy|wtf", re.I)),
    ("Exciting moment", "Great for gaming & sports", re.compile(r"let'?s go|clutch|win|backflip|play|goal|gta|insane", re.I)),
    ("Funny moment", "Good for entertainment", re.compile(r"lol|lmao|haha|bro|joke|chopped|weirdo|funny", re.I)),
    ("Interesting statement", "Creates engaging clips", re.compile(r"truth|honest|secret|actually|always|never|real", re.I)),
    ("Surprising info", "Makes people curious", re.compile(r"wait|really|found out|didn'?t know|years|\$", re.I)),
    ("Controversial opinion", "Encourages discussion", re.compile(r"overrated|trash|wrong|disagree|unpopular|bad|worst", re.I)),
    ("Story with payoff", "Keeps viewers watching", re.compile(r"story|happened|remember|then|finally|progress", re.I)),
    ("Important explanation", "Creates educational clips", re.compile(r"because|how to|reason|means|step|tip|here'?s why", re.I)),
]


def make_catchy_title(cat: str, idx: int, snippet: str, base_title: str) -> str:
    if snippet:
        words = re.sub(r"\s+", " ", snippet).strip().split(" ")
        short = " ".join(words[:7])
        return short + ("..." if len(words) > 7 else "!")
    prefixes = {
        "Strong reaction": "Wait—Watch This Reaction",
        "Exciting moment": "Insane Highlight Moment",
        "Funny moment": "Funniest Moment of the Stream",
        "Interesting statement": "Nobody Talks About This",
        "Surprising info": "You Won't Expect This",
        "Controversial opinion": "Hot Take That Sparks Debate",
        "Story with payoff": "Watch Until the Payoff",
        "Important explanation": "Key Breakdown Explained",
    }
    short_base = re.sub(r"\s+", " ", base_title).strip()[:26]
    return f"{prefixes.get(cat, cat)} #{idx + 1} ({short_base})" if short_base else f"{prefixes.get(cat, cat)} #{idx + 1}"


def compute_6_factor_score(heat_pct: float, chat_msgs: int, max_chat: int, dur: float, snippet: str) -> tuple[float, dict]:
    has_hook_words = bool(re.search(r"wait|look|crazy|no way|what|holy|why|\?|!", snippet or "", re.I))
    hook = min(25, round(15 + (heat_pct / 100.0) * 8 + (2 if has_hook_words else 0)))
    ent = min(20, round(12 + (chat_msgs / max(1, max_chat)) * 8))
    emo = min(15, round(10 + ((heat_pct / 100.0) * 3) + (2 if chat_msgs > 0 else 0)))
    story = 14 if 20 <= dur <= 60 else 11
    ret = 14 if dur <= 45 else 11
    av = 9
    total = min(99.0, round(hook + ent + emo + story + ret + av, 1))
    return total, {"hook": hook, "ent": ent, "emo": emo, "story": story, "ret": ret, "av": av}


def get_local_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            text=True,
        ).strip()
        return float(out)
    except Exception:
        return 60.0


def parse_vtt_snippets(vtt_path: Path) -> list[dict]:
    entries = []
    try:
        raw = vtt_path.read_text(encoding="utf-8", errors="ignore")
        blocks = re.split(r"\n\s*\n", raw)
        ts_re = re.compile(r"(\d{2}:)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}:)?(\d{2}):(\d{2})\.(\d{3})")
        for b in blocks:
            m = ts_re.search(b)
            if not m:
                continue
            h1 = int(m.group(1)[:-1]) if m.group(1) else 0
            st = h1 * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0
            h2 = int(m.group(5)[:-1]) if m.group(5) else 0
            en = h2 * 3600 + int(m.group(6)) * 60 + int(m.group(7)) + int(m.group(8)) / 1000.0
            lines = [re.sub(r"<[^>]+>", "", ln).strip() for ln in b.splitlines() if "-->" not in ln and not ln.strip().isdigit()]
            txt = " ".join(ln for ln in lines if ln and not ln.startswith(("WEBVTT", "Kind:", "Language:")))
            if txt and en > st:
                entries.append({"start": st, "end": en, "text": txt})
    except Exception:
        pass
    return entries


def analyze_job(job_id: str, payload: dict) -> None:
    job = ANALYZE_JOBS[job_id]
    try:
        source = payload["source"]
        is_local = bool(payload.get("is_local", False))
        clip_len = int(payload.get("clip_len", 35))
        top_n = int(payload.get("top_n", 10))
        cookies_text = (payload.get("cookies") or "").strip()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            cookies_path = None
            if cookies_text:
                cookies_path = tmp_dir / "cookies.txt"
                cookies_path.write_text(cookies_text, encoding="utf-8")

            vtt_entries: list[dict] = []
            chat_buckets: Counter[int] = Counter()
            heatmap = []

            if is_local:
                p = Path(source)
                job["status"] = "Scanning entire uploaded video..."
                duration = get_local_duration(p)
                title = p.name
            else:
                job["status"] = "Opening video & scanning full timeline..."
                info = None
                last_err = "Could not open video link"
                for browser in get_browser_candidates():
                    proc = subprocess.run(
                        ytdlp_base_cmd(cookies_path, browser) + ["--dump-json", "--skip-download", source],
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        info = json.loads(proc.stdout)
                        break
                    if proc.stderr or proc.stdout:
                        last_err = (proc.stderr or proc.stdout).strip().splitlines()[-1]

                if info is None and ("youtube.com" in source or "youtu.be" in source):
                    try:
                        from pytubefix import YouTube
                        yt = YouTube(source)
                        info = {"title": yt.title or "YouTube Video", "duration": yt.length or 60, "heatmap": []}
                    except Exception as exc:
                        last_err = f"{last_err} | pytubefix: {exc}"

                if info is None:
                    raise RuntimeError(last_err)

                title = info.get("title") or "Untitled Video"
                duration = float(info.get("duration") or 60)
                heatmap = info.get("heatmap") or []

                job["status"] = "Analyzing full transcript & live chat spikes..."
                for browser in get_browser_candidates():
                    proc_sub = subprocess.run(
                        ytdlp_base_cmd(cookies_path, browser) + [
                            "--skip-download", "--write-auto-subs", "--write-subs",
                            "--sub-langs", "en,live_chat",
                            "-o", str(tmp_dir / "meta"), source,
                        ],
                        check=False,
                        timeout=60,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if proc_sub.returncode == 0:
                        break

                for vtt_file in tmp_dir.glob("*.vtt"):
                    vtt_entries.extend(parse_vtt_snippets(vtt_file))
                for chat_file in tmp_dir.glob("*.live_chat.json"):
                    with chat_file.open("r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            try:
                                obj = json.loads(line)
                                offset_ms = (
                                    obj.get("replayChatItemAction", {}).get("videoOffsetTimeMsec")
                                    or obj.get("videoOffsetTimeMsec")
                                )
                                if offset_ms is not None:
                                    chat_buckets[int(float(offset_ms) // 10000)] += 1
                            except Exception:
                                continue

            job["status"] = "Scoring & ranking top viral moments..."
            candidates = []
            max_chat = max(chat_buckets.values(), default=1)

            def get_snippet(st: float, en: float) -> str:
                return " ".join(e["text"] for e in vtt_entries if e["start"] >= st and e["start"] <= en)[:180]

            if heatmap:
                for idx, pt in enumerate(heatmap):
                    t_mid = (float(pt["start_time"]) + float(pt["end_time"])) / 2.0
                    heat_pct = round(float(pt.get("value", 0.0)) * 100, 1)
                    bucket = int(t_mid // 10)
                    chat_cnt = chat_buckets[bucket]
                    start_s = max(0.0, t_mid - clip_len * 0.35)
                    end_s = min(duration or (start_s + clip_len), start_s + clip_len)
                    snip = get_snippet(start_s, end_s)
                    cat, why, _ = next((d for d in DETECTORS if snip and d[2].search(snip)), DETECTORS[idx % len(DETECTORS)])
                    score, breakdown = compute_6_factor_score(heat_pct, chat_cnt, max_chat, end_s - start_s, snip)
                    candidates.append({
                        "title": make_catchy_title(cat, idx, snip, title),
                        "category": cat,
                        "reason": why,
                        "start": round(start_s, 1),
                        "end": round(end_s, 1),
                        "start_hms": sec_to_hms(start_s),
                        "end_hms": sec_to_hms(end_s),
                        "score": score,
                        "breakdown": breakdown,
                        "heat": heat_pct,
                        "chat_msgs": chat_cnt,
                    })
            elif chat_buckets:
                for idx, (bucket, count) in enumerate(chat_buckets.most_common(top_n * 4)):
                    t_mid = bucket * 10.0
                    start_s = max(0.0, t_mid - clip_len * 0.35)
                    end_s = min(duration or (start_s + clip_len), start_s + clip_len)
                    snip = get_snippet(start_s, end_s)
                    cat, why, _ = next((d for d in DETECTORS if snip and d[2].search(snip)), DETECTORS[idx % len(DETECTORS)])
                    score, breakdown = compute_6_factor_score(0.0, count, max_chat, end_s - start_s, snip)
                    candidates.append({
                        "title": make_catchy_title(cat, idx, snip, title),
                        "category": cat,
                        "reason": why,
                        "start": round(start_s, 1),
                        "end": round(end_s, 1),
                        "start_hms": sec_to_hms(start_s),
                        "end_hms": sec_to_hms(end_s),
                        "score": score,
                        "breakdown": breakdown,
                        "heat": 0.0,
                        "chat_msgs": count,
                    })

            candidates.sort(key=lambda c: c["score"], reverse=True)
            picked = []
            for cand in candidates:
                if any(abs(cand["start"] - p["start"]) < clip_len * 0.75 for p in picked):
                    continue
                picked.append(cand)
                if len(picked) >= top_n:
                    break

            if not picked and duration > 0:
                count = min(top_n, max(1, int(duration // max(10, clip_len)) or 1))
                step = duration / count
                for i in range(count):
                    st = round(i * step, 1)
                    en = round(min(duration, st + clip_len), 1)
                    if en - st < 3:
                        continue
                    snip = get_snippet(st, en)
                    cat, why, _ = next((d for d in DETECTORS if snip and d[2].search(snip)), DETECTORS[i % len(DETECTORS)])
                    score, breakdown = compute_6_factor_score(65 - i * 4, 0, 1, en - st, snip)
                    picked.append({
                        "title": make_catchy_title(cat, i, snip, title),
                        "category": cat,
                        "reason": why,
                        "start": st,
                        "end": en,
                        "start_hms": sec_to_hms(st),
                        "end_hms": sec_to_hms(en),
                        "score": score,
                        "breakdown": breakdown,
                        "heat": 0.0,
                        "chat_msgs": 0,
                    })

            job["done"] = True
            job["status"] = f"Found {len(picked)} clips across the full video!"
            job["result"] = {"title": title, "duration": duration, "duration_hms": sec_to_hms(duration), "clips": picked}
    except Exception as exc:
        job["done"] = True
        job["error"] = str(exc)
        job["status"] = f"Error: {exc}"


def build_ass_subtitles(words: list[dict], ass_path: Path, hook: str, hook_dur: float, style_preset: str) -> None:
    preset_colors = {
        "hormozi": ("&H0000FFFF", "&H00FFFFFF", 84, 1),
        "beast": ("&H0080FF00", "&H00FFFFFF", 82, 1),
        "karaoke": ("&H00FFFF00", "&H00E4E4E7", 80, 1),
        "minimal": ("&H00FFFFFF", "&H00D4D4D8", 68, 2),
        "opus1word": ("&H00FFFFFF", "&H00FFFFFF", 88, 1),
    }
    prim_col, _, font_sz, words_per_line = preset_colors.get(style_preset, preset_colors["hormozi"])

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: PopWord,DejaVu Sans,{font_sz},{prim_col},&H0000FFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,7,4,2,60,60,240,1
Style: HookBox,DejaVu Sans,50,&H0009090B,&H0009090B,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,28,0,5,80,80,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    if hook.strip():
        safe_hook = hook.strip().upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 1,0:00:00.00,{format_ass_time(hook_dur)},HookBox,,0,0,0,,{{\\pos(540,640)}}{safe_hook}\n")

    for i in range(0, len(words), words_per_line):
        chunk = words[i:i + words_per_line]
        st = format_ass_time(chunk[0]["start"])
        en = format_ass_time(max(chunk[-1]["end"], chunk[0]["start"] + 0.14))
        txt = " ".join(w["text"] for w in chunk).upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{st},{en},PopWord,,0,0,0,,{txt}\n")

    ass_path.write_text("".join(lines), encoding="utf-8")


def render_job(job_id: str, payload: dict) -> None:
    job = JOBS[job_id]
    try:
        source = payload["source"]
        is_local = bool(payload.get("is_local", False))
        start_hms = payload["start_hms"]
        end_hms = payload["end_hms"]
        layout = payload.get("layout", "split")
        cam = payload.get("cam", "top-left")
        hook = payload.get("hook", "")
        hook_dur = float(payload.get("hook_dur", 3.5))
        caption_style = payload.get("caption_style", "hormozi")
        cookies_text = (payload.get("cookies") or "").strip()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            cookies_path = None
            if cookies_text:
                cookies_path = tmp_dir / "cookies.txt"
                cookies_path.write_text(cookies_text, encoding="utf-8")

            src_mp4 = tmp_dir / "source.mp4"
            if is_local:
                job["status"] = "Trimming uploaded video section..."
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", start_hms, "-to", end_hms, "-i", source, "-c", "copy", str(src_mp4)],
                    check=True,
                )
            else:
                job["status"] = "Downloading clip section..."
                robust_ytdlp_download(source, start_hms, end_hms, src_mp4, cookies_path)

            words = []
            try:
                from faster_whisper import WhisperModel
                job["status"] = "Transcribing word-by-word captions (Whisper)..."
                model = WhisperModel("base", device="auto", compute_type="int8")
                segments, _ = model.transcribe(str(src_mp4), word_timestamps=True)
                for seg in segments:
                    for w in (seg.words or []):
                        t = w.word.strip()
                        if t:
                            words.append({"start": w.start, "end": w.end, "text": t})
            except ImportError:
                job["status"] = "Whisper not installed (rendering without captions)..."

            ass_path = None
            if words or hook.strip():
                ass_path = tmp_dir / "captions.ass"
                build_ass_subtitles(words, ass_path, hook, hook_dur, caption_style)

            job["status"] = "Rendering iPhone & TikTok 1080x1920 MP4..."
            out_name = f"clipforge_{job_id[:8]}.mp4"
            out_path = OUT_DIR / out_name

            if layout == "split":
                cam_crop_map = {
                    "top-left": "crop=iw*0.32:ih*0.36:0:0",
                    "top-right": "crop=iw*0.32:ih*0.36:iw-iw*0.32:0",
                    "bottom-left": "crop=iw*0.32:ih*0.36:0:ih-ih*0.36",
                    "bottom-right": "crop=iw*0.32:ih*0.36:iw-iw*0.32:ih-ih*0.36",
                    "center": "crop=iw*0.32:ih*0.36:(iw-iw*0.32)/2:(ih-ih*0.36)/2",
                }
                cam_crop = cam_crop_map.get(cam, cam_crop_map["top-left"])
                vf = (
                    f"[0:v]{cam_crop},scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640[top];"
                    f"[0:v]scale=1080:1280:force_original_aspect_ratio=increase,crop=1080:1280[bot];"
                    f"[top][bot]vstack=inputs=2[stacked]"
                )
                base_label = "stacked"
            elif layout == "blur":
                vf = (
                    "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
                    "[0:v]scale=1080:-2[fg];"
                    "[bg][fg]overlay=(W-w)/2:(H-h)/2[stacked]"
                )
                base_label = "stacked"
            else:
                vf = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[stacked]"
                base_label = "stacked"

            if ass_path:
                esc = str(ass_path).replace("\\", "/").replace(":", "\\:")
                vf += f";[{base_label}]ass='{esc}'[vout]"
            else:
                vf += f";[{base_label}]null[vout]"

            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(src_mp4),
                    "-filter_complex", vf,
                    "-map", "[vout]", "-map", "0:a?",
                    "-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
                    "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                    "-movflags", "+faststart",
                    str(out_path),
                ],
                check=True,
            )

            job["done"] = True
            job["status"] = "Ready! Formatted for TikTok, Reels, Shorts & iPhone."
            job["file"] = out_name
    except Exception as exc:
        job["done"] = True
        job["error"] = str(exc)
        job["status"] = f"Error: {exc}"


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ClipForge Pro — Free OpusClip Alternative</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#09090f] text-zinc-100 min-h-screen font-sans">
  <header class="border-b border-white/10 bg-[#0d0d16] px-4 sm:px-6 py-3 flex flex-wrap items-center justify-between gap-2">
    <div class="flex items-center gap-2.5">
      <div class="w-8 h-8 rounded-lg bg-gradient-to-br from-violet-500 to-fuchsia-500 flex items-center justify-center font-bold text-sm">CF</div>
      <div>
        <h1 class="font-bold text-base">ClipForge Pro <span class="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 ml-1">Unlimited • Free • No Watermark</span></h1>
        <p class="text-[11px] text-zinc-400">Scans full videos from YouTube, Twitch, Kick, TikTok, X, or Upload → 1080×1920 iPhone & TikTok MP4</p>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto p-3 sm:p-5 space-y-4">
    <section class="bg-[#12121e] border border-white/10 rounded-xl p-4 space-y-3.5">
      <div class="flex flex-col sm:flex-row gap-2">
        <input id="urlInput" type="url" placeholder="Paste YouTube, YouTube Live, Twitch, Kick, TikTok, X, or direct MP4 link..." class="flex-1 bg-[#181828] border border-white/10 rounded-lg px-3.5 py-2.5 text-sm text-white focus:outline-none focus:border-violet-500" />
        <button id="analyzeBtn" class="px-5 py-2.5 rounded-lg bg-violet-600 hover:bg-violet-500 font-semibold text-sm transition whitespace-nowrap">Find Best Clips</button>
        <label class="cursor-pointer px-4 py-2.5 rounded-lg bg-white/10 hover:bg-white/15 font-semibold text-sm transition text-center whitespace-nowrap">
          Upload Video
          <input id="uploadInput" type="file" accept="video/*" class="hidden" />
        </label>
      </div>

      <div class="grid grid-cols-2 sm:grid-cols-5 gap-2.5 text-xs">
        <div>
          <label class="text-zinc-400 block mb-1">Layout</label>
          <select id="layoutSelect" class="w-full bg-[#181828] border border-white/10 rounded-lg px-2 py-2">
            <option value="split" selected>Top 1/3 Cam + 2/3 Game</option>
            <option value="fill">Fill (9:16 Center)</option>
            <option value="blur">Blur BG (Fit)</option>
          </select>
        </div>
        <div>
          <label class="text-zinc-400 block mb-1">Facecam Corner</label>
          <select id="camSelect" class="w-full bg-[#181828] border border-white/10 rounded-lg px-2 py-2">
            <option value="top-left" selected>Top-Left</option>
            <option value="top-right">Top-Right</option>
            <option value="bottom-left">Bottom-Left</option>
            <option value="bottom-right">Bottom-Right</option>
            <option value="center">Center</option>
          </select>
        </div>
        <div>
          <label class="text-zinc-400 block mb-1">Caption Style</label>
          <select id="captionStyleSelect" class="w-full bg-[#181828] border border-white/10 rounded-lg px-2 py-2">
            <option value="hormozi" selected>Hormozi Bold</option>
            <option value="beast">Beast Pop</option>
            <option value="opus1word">Opus 1-Word Pop</option>
            <option value="karaoke">Neon Karaoke</option>
            <option value="minimal">Minimal Clean</option>
          </select>
        </div>
        <div>
          <label class="text-zinc-400 block mb-1">Clip Length</label>
          <select id="clipLenSelect" class="w-full bg-[#181828] border border-white/10 rounded-lg px-2 py-2">
            <option value="15">15s (Ultra Short)</option>
            <option value="30" selected>30s (TikTok/Shorts)</option>
            <option value="45">45s (Story)</option>
            <option value="60">60s (Deep Dive)</option>
          </select>
        </div>
        <div class="col-span-2 sm:col-span-1">
          <label class="text-zinc-400 block mb-1">Hook Banner</label>
          <input id="hookInput" type="text" value="GTA 6 HAS US LOSING IT" class="w-full bg-[#181828] border border-white/10 rounded-lg px-2.5 py-2" />
        </div>
      </div>

      <details class="text-xs text-zinc-400 pt-1">
        <summary class="cursor-pointer hover:text-zinc-200">Optional: Paste YouTube cookies.txt (if YouTube blocks cloud IP)</summary>
        <textarea id="cookiesInput" rows="2" placeholder="Paste Netscape cookies.txt content here only if YouTube asks to sign in..." class="mt-2 w-full bg-[#181828] border border-white/10 rounded-lg p-2 text-[11px] text-zinc-200"></textarea>
      </details>
    </section>

    <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
      <section class="lg:col-span-6 bg-[#12121e] border border-white/10 rounded-xl p-4 space-y-3">
        <div class="flex items-center justify-between">
          <h2 class="font-semibold text-sm">Ranked AI Clips (6-Factor Score)</h2>
          <span class="text-[10px] text-zinc-400">Estimates only, not guarantees</span>
        </div>
        <div id="videoMeta" class="text-xs text-zinc-300">Paste any video link or upload a video file above.</div>
        <div id="clipsList" class="space-y-2.5 max-h-[540px] overflow-y-auto pr-1"></div>
      </section>

      <section class="lg:col-span-6 bg-[#12121e] border border-white/10 rounded-xl p-4 flex flex-col items-center justify-center min-h-[460px] space-y-3">
        <div id="renderStatus" class="text-xs text-violet-300 font-medium text-center">Select a clip and click Render MP4.</div>
        <video id="outputVideo" controls playsinline class="hidden max-h-[480px] rounded-xl border border-white/15 bg-black"></video>
        <a id="downloadLink" href="#" download class="hidden px-5 py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-xs font-semibold">Download iPhone / TikTok MP4</a>
      </section>
    </div>
  </main>

  <script>
    let activeSource = '';
    let activeIsLocal = false;

    const clipsList = document.getElementById('clipsList');
    const videoMeta = document.getElementById('videoMeta');
    const renderStatus = document.getElementById('renderStatus');
    const outputVideo = document.getElementById('outputVideo');
    const downloadLink = document.getElementById('downloadLink');

    function hmsToSec(hms) {
      const p = hms.split(':').map(Number);
      return p.length === 3 ? p[0]*3600 + p[1]*60 + p[2] : 0;
    }
    function secToHms(sec) {
      sec = Math.max(0, Math.floor(sec));
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
      return [h, m, s].map(v => String(v).padStart(2, '0')).join(':');
    }

    async function startRender(start_hms, end_hms) {
      const src = activeIsLocal ? activeSource : document.getElementById('urlInput').value.trim();
      if (!src) {
        renderStatus.textContent = 'Please paste a video link or upload a video first.';
        return;
      }
      renderStatus.textContent = 'Starting render...';
      outputVideo.classList.add('hidden');
      downloadLink.classList.add('hidden');

      try {
        const res = await fetch('/api/render', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source: src,
            is_local: activeIsLocal,
            start_hms,
            end_hms,
            layout: document.getElementById('layoutSelect').value,
            cam: document.getElementById('camSelect').value,
            caption_style: document.getElementById('captionStyleSelect').value,
            hook: document.getElementById('hookInput').value,
            cookies: document.getElementById('cookiesInput').value
          })
        });
        const { job_id } = await res.json();
        const poll = setInterval(async () => {
          try {
            const jRes = await fetch('/api/job?id=' + encodeURIComponent(job_id));
            const job = await jRes.json();
            renderStatus.textContent = job.status;
            if (job.done) {
              clearInterval(poll);
              if (job.file) {
                const mediaUrl = '/exports/' + encodeURIComponent(job.file);
                outputVideo.src = mediaUrl;
                outputVideo.classList.remove('hidden');
                downloadLink.href = mediaUrl;
                downloadLink.download = job.file;
                downloadLink.classList.remove('hidden');
              }
            }
          } catch (e) {
            clearInterval(poll);
            renderStatus.textContent = 'Connection error: ' + e.message;
          }
        }, 1200);
      } catch (e) {
        renderStatus.textContent = 'Render failed: ' + e.message;
      }
    }

    function renderClips(data) {
      videoMeta.textContent = `${data.title} (${data.duration_hms})`;
      clipsList.innerHTML = '';
      data.clips.forEach((c) => {
        const card = document.createElement('div');
        card.className = 'p-3 rounded-lg bg-[#181828] border border-white/10 space-y-2';
        card.innerHTML = `
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-1.5">
                <span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-400">${c.score} Est. Viral</span>
                <span class="px-1.5 py-0.5 rounded text-[10px] bg-violet-500/20 text-violet-300">${c.category}</span>
              </div>
              <div class="text-xs font-semibold text-white mt-1">${c.title}</div>
              <div class="text-[10px] text-zinc-400">${c.reason} • <span class="time-range font-mono">${c.start_hms} → ${c.end_hms}</span></div>
            </div>
            <button class="render-btn shrink-0 px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 text-xs font-semibold">Render MP4</button>
          </div>
          <div class="grid grid-cols-6 gap-1 text-[9px] text-zinc-400 font-mono">
            <span class="bg-white/5 rounded px-1 py-0.5 text-center">Hook ${c.breakdown.hook}/25</span>
            <span class="bg-white/5 rounded px-1 py-0.5 text-center">Ent ${c.breakdown.ent}/20</span>
            <span class="bg-white/5 rounded px-1 py-0.5 text-center">Emo ${c.breakdown.emo}/15</span>
            <span class="bg-white/5 rounded px-1 py-0.5 text-center">Story ${c.breakdown.story}/15</span>
            <span class="bg-white/5 rounded px-1 py-0.5 text-center">Ret ${c.breakdown.ret}/15</span>
            <span class="bg-white/5 rounded px-1 py-0.5 text-center">A/V ${c.breakdown.av}/10</span>
          </div>
          <div class="flex items-center gap-1.5 pt-1">
            <span class="text-[10px] text-zinc-400">Length:</span>
            ${[15, 30, 45, 60].map(l => `<button type="button" data-len="${l}" class="len-btn px-2 py-0.5 rounded text-[10px] bg-white/5 hover:bg-violet-600 text-zinc-200">${l}s</button>`).join('')}
          </div>
        `;
        card.querySelectorAll('.len-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            const st = hmsToSec(c.start_hms);
            c.end_hms = secToHms(st + Number(btn.dataset.len));
            card.querySelector('.time-range').textContent = `${c.start_hms} → ${c.end_hms}`;
          });
        });
        card.querySelector('.render-btn').addEventListener('click', () => startRender(c.start_hms, c.end_hms));
        clipsList.appendChild(card);
      });
    }

    async function runAnalyze(source, isLocal) {
      videoMeta.textContent = 'Starting full-video scan...';
      clipsList.innerHTML = '';
      try {
        const res = await fetch('/api/analyze', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source,
            is_local: isLocal,
            clip_len: Number(document.getElementById('clipLenSelect').value),
            cookies: document.getElementById('cookiesInput').value
          })
        });
        const { job_id } = await res.json();
        const poll = setInterval(async () => {
          try {
            const jRes = await fetch('/api/analyze_job?id=' + encodeURIComponent(job_id));
            const job = await jRes.json();
            videoMeta.textContent = job.status;
            if (job.done) {
              clearInterval(poll);
              if (job.error) {
                videoMeta.textContent = 'Error: ' + job.error;
              } else if (job.result) {
                renderClips(job.result);
              }
            }
          } catch (e) {
            clearInterval(poll);
            videoMeta.textContent = 'Polling error: ' + e.message;
          }
        }, 1000);
      } catch (e) {
        videoMeta.textContent = 'Request failed: ' + e.message;
      }
    }

    document.getElementById('analyzeBtn').addEventListener('click', () => {
      const url = document.getElementById('urlInput').value.trim();
      if (!url) {
        videoMeta.textContent = 'Please paste a YouTube, Twitch, Kick, TikTok, or video link first.';
        return;
      }
      activeSource = url;
      activeIsLocal = false;
      runAnalyze(url, false);
    });

    document.getElementById('uploadInput').addEventListener('change', async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      videoMeta.textContent = `Uploading ${file.name}...`;
      try {
        const res = await fetch('/api/upload?name=' + encodeURIComponent(file.name), { method: 'POST', body: file });
        const up = await res.json();
        if (up.error) { videoMeta.textContent = 'Upload error: ' + up.error; return; }
        activeSource = up.path;
        activeIsLocal = true;
        runAnalyze(up.path, true);
      } catch (err) {
        videoMeta.textContent = 'Upload failed: ' + err.message;
      }
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            data = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == "/api/job":
            qs = parse_qs(parsed.query)
            job_id = qs.get("id", [""])[0]
            self._send_json(JOBS.get(job_id, {"done": True, "status": "Not found"}))
        elif parsed.path == "/api/analyze_job":
            qs = parse_qs(parsed.query)
            job_id = qs.get("id", [""])[0]
            self._send_json(ANALYZE_JOBS.get(job_id, {"done": True, "status": "Not found"}))
        elif parsed.path.startswith("/exports/"):
            fname = Path(parsed.path.replace("/exports/", "", 1)).name
            fpath = OUT_DIR / fname
            if not fpath.exists():
                self.send_error(404)
                return
            data = fpath.read_bytes()
            mime = mimetypes.guess_type(str(fpath))[0] or "video/mp4"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if parsed.path == "/api/upload":
            qs = parse_qs(parsed.query)
            raw_name = Path(qs.get("name", ["upload.mp4"])[0]).name
            dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{raw_name}"
            with dest.open("wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            self._send_json({"path": str(dest)})
            return

        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if parsed.path == "/api/analyze":
            job_id = uuid.uuid4().hex
            ANALYZE_JOBS[job_id] = {"done": False, "status": "Starting full-video scan..."}
            threading.Thread(target=analyze_job, args=(job_id, body), daemon=True).start()
            self._send_json({"job_id": job_id})
        elif parsed.path == "/api/render":
            job_id = uuid.uuid4().hex
            JOBS[job_id] = {"done": False, "status": "Queued..."}
            threading.Thread(target=render_job, args=(job_id, body), daemon=True).start()
            self._send_json({"job_id": job_id})
        else:
            self.send_error(404)


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"ClipForge Pro running at {url}")
    if HOST == "127.0.0.1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


