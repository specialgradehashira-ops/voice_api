import io
import os
import re
import tempfile
import subprocess
from flask import Flask, request, send_file, jsonify
import edge_tts
import imageio_ffmpeg

app = Flask(__name__)

# Get a static ffmpeg binary (downloaded/cached by imageio-ffmpeg)
FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()

# Voices you can allow from the sheet/API; fallback if not provided
VOICES_ALLOWED = {
    "en-US-JennyNeural",
    "en-US-GuyNeural",
    "en-GB-LibbyNeural",
}


def ffmpeg_duration_seconds(path: str) -> float:
    """
    Read media duration by parsing ffmpeg stderr ('Duration: 00:00:10.12').
    We only have ffmpeg here (not ffprobe), so we parse the banner.
    """
    try:
        proc = subprocess.run(
            [FFMPEG_BIN, "-i", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr or "")
        if not m:
            return 0.0
        h, mnt, sec = m.groups()
        return int(h) * 3600 + int(mnt) * 60 + float(sec)
    except Exception:
        return 0.0


async def tts_to_mp3(text: str, voice: str, rate: str, volume: str) -> str:
    """
    Generate MP3 from (possibly long) text using edge-tts.
    We chunk the text to ~3800 chars to keep it reliable, and then
    concatenate all MP3 parts with the ffmpeg concat demuxer (no re-encode).
    """
    # Normalize text (edge-tts accepts raw punctuation fine)
    t = (text or "").strip()
    if not t:
        t = " "  # avoid empty input error

    # Chunk the text around ~3800 characters, preferring sentence boundaries.
    chunks = []
    CHUNK = 3800
    while t:
        c = t[:CHUNK]
        cut = c.rfind(". ")
        if cut > 1200:  # prefer ending on a sentence if chunk is long enough
            c = c[:cut + 1]
        chunks.append(c)
        t = t[len(c):]

    part_paths = []
    for c in chunks:
        # Stream TTS audio bytes into memory, then write as MP3
        communicate = edge_tts.Communicate(
            c,
            voice=voice if voice in VOICES_ALLOWED else "en-US-JennyNeural",
            rate=rate or "0%",
            volume=volume or "0%",
        )
        buf = io.BytesIO()
        async for part in communicate.stream():
            if part[0] == "audio":
                buf.write(part[1])
        buf.seek(0)

        part = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        with open(part.name, "wb") as f:
            f.write(buf.getvalue())
        part_paths.append(part.name)

    if len(part_paths) == 1:
        return part_paths[0]

    # Concatenate MP3 parts losslessly with ffmpeg concat demuxer
    list_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    with open(list_file, "w", encoding="utf-8") as f:
        for p in part_paths:
            # Escape single quotes in path for concat file format
            f.write(f"file '{p.replace(\"'\", \"'\\\\''\")}'\n")

    out_mp3 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        out_mp3
    ]
    subprocess.check_call(cmd)
    return out_mp3


def mux_loop_or_trim(video_path: str, audio_path: str) -> str:
    """
    If audio is longer than video, loop the video until audio ends (stop at audio).
    If audio is shorter, trim the video to match audio (stop at audio).
    We always encode audio to AAC; video is copied (no re-encode).
    """
    vdur = ffmpeg_duration_seconds(video_path)
    adur = ffmpeg_duration_seconds(audio_path)

    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name

    # audio longer → loop video
    if adur > vdur + 0.05:
        cmd = [
            FFMPEG_BIN, "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",  # keep original video
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",      # stop when shortest of the two ends → audio controls length
            out_path
        ]
    else:
        # audio shorter or almost equal → trim video to audio
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            out_path
        ]
    subprocess.check_call(cmd)
    return out_path


@app.get("/")
def root():
    return {"ok": True, "endpoint": "/process-video"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/process-video")
def process_video():
    """
    Form-Data:
      - video: file (mp4)
      - text: string (voiceover text)
      - voice (optional): one of VOICES_ALLOWED (default Jenny)
      - rate (optional): e.g. +10%, -5%
      - volume (optional): e.g. +5%, -10%
    Returns: MP4 with voiceover; video loops or trims to match audio.
    """
    try:
        upfile = next(iter(request.files.values()), None)
        if not upfile:
            return jsonify({"error": "No video file provided (form field 'video')"}), 400

        text = (request.form.get("text") or "").strip()
        if not text:
            text = " "

        voice = request.form.get("voice", "en-US-JennyNeural")
        rate = request.form.get("rate", "0%")
        volume = request.form.get("volume", "0%")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            upfile.save(vf.name)
            video_path = vf.name

        # 1) TTS → MP3 (handles long text via chunking + concat)
        # NOTE: edge-tts is async; run it in a one-off event loop per request
        import asyncio
        audio_mp3 = asyncio.run(tts_to_mp3(text, voice=voice, rate=rate, volume=volume))

        # 2) Mux with loop/trim logic
        out_path = mux_loop_or_trim(video_path, audio_mp3)

        return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="output.mp4")

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Flask dev server is fine on Render free tier
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
