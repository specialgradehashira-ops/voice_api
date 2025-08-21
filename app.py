import io
import os
import re
import tempfile
import subprocess
from flask import Flask, request, send_file, jsonify
import edge_tts
import imageio_ffmpeg

app = Flask(__name__)

# Grab a static ffmpeg binary that imageio-ffmpeg downloads/caches
FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()

VOICES_ALLOWED = {
    "en-US-JennyNeural",
    "en-US-GuyNeural",
    "en-GB-LibbyNeural",
}


def ffmpeg_duration_seconds(path: str) -> float:
    """
    Read media duration by parsing ffmpeg stderr ('Duration: 00:00:10.12').
    We only rely on ffmpeg (not ffprobe) here.
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
    Chunk to ~3800 chars and concatenate parts losslessly with ffmpeg.
    """
    t = (text or "").strip()
    if not t:
        t = " "

    # Chunk around ~3800 chars, prefer to end on a sentence boundary
    chunks = []
    CHUNK = 3800
    while t:
        c = t[:CHUNK]
        cut = c.rfind(". ")
        if cut > 1200:
            c = c[:cut + 1]
        chunks.append(c)
        t = t[len(c):]

    part_paths = []
    for c in chunks:
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

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        with open(tmp.name, "wb") as f:
            f.write(buf.getvalue())
        part_paths.append(tmp.name)

    if len(part_paths) == 1:
        return part_paths[0]

    # Concat demuxer list file: our /tmp/* paths have no spaces/quotes, so no escaping needed
    list_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    with open(list_file, "w", encoding="utf-8") as f:
        for p in part_paths:
            f.write(f"file {p}\n")

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
    If audio > video, loop video to match audio; else trim video to audio.
    Video stream is copied; audio encoded to AAC.
    """
    vdur = ffmpeg_duration_seconds(video_path)
    adur = ffmpeg_duration_seconds(audio_path)

    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name

    if adur > vdur + 0.05:
        cmd = [
            FFMPEG_BIN, "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            out_path
        ]
    else:
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

        import asyncio
        audio_mp3 = asyncio.run(tts_to_mp3(text, voice=voice, rate=rate, volume=volume))

        out_path = mux_loop_or_trim(video_path, audio_mp3)

        return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="output.mp4")

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
