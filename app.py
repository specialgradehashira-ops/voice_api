import io
import os
import tempfile
import asyncio
import subprocess
from typing import List
from flask import Flask, request, send_file, jsonify
import edge_tts
import imageio_ffmpeg

app = Flask(__name__)

# ffmpeg binary from imageio-ffmpeg (bundled wheel)
FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
# ffprobe is available on Render’s base image; use plain name.
FFPROBE_BIN = "ffprobe"

# Tunables (you can override via Render → Environment)
TEXT_MAX_CHARS = int(os.environ.get("TEXT_MAX_CHARS", "20000"))          # hard limit per request
TTS_CHARS_PER_CHUNK = int(os.environ.get("TTS_CHARS_PER_CHUNK", "2200")) # smaller = lower memory spikes


def ffprobe_duration(path: str) -> float:
    """Return media duration in seconds."""
    try:
        out = subprocess.check_output([
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]).decode().strip()
        return float(out)
    except Exception:
        return 0.0


def video_has_audio(path: str) -> bool:
    """True if input file has an audio stream."""
    try:
        out = subprocess.check_output([
            FFPROBE_BIN, "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            path,
        ]).decode().strip()
        return bool(out)
    except Exception:
        return False


def normalize_rate(val: str) -> str:
    """Accept '0%', '+5%', '-10%', or integers like '180' (treated as %)."""
    if not val:
        return "0%"
    s = str(val).strip()
    if s.endswith("%"):
        return s
    # try number, convert to percent string
    try:
        n = int(float(s))
        return f"{n:+d}%"
    except Exception:
        return "0%"


async def tts_to_mp3_streaming(text: str, voice="en-US-JennyNeural", rate="0%", volume="0%") -> str:
    """
    Generate MP3 using Edge TTS.
    Streams audio bytes directly to a temp file per chunk (no large in-RAM buffers),
    then concatenates chunks losslessly with ffmpeg concat demuxer.
    """
    t = (text or " ").strip()
    # Split into safe chunks
    chunks: List[str] = []
    while t:
        c = t[:TTS_CHARS_PER_CHUNK]
        # prefer to cut on sentence boundary if chunk is reasonably long
        cut = c.rfind(". ")
        if cut > 1000:
            c = c[:cut + 1]
        chunks.append(c)
        t = t[len(c):]

    part_paths: List[str] = []
    for c in chunks:
        communicate = edge_tts.Communicate(
            c,
            voice=voice,
            rate=rate,
            volume=volume,
        )
        # Stream straight to disk
        part_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        try:
            async for part in communicate.stream():
                if part[0] == "audio":
                    part_file.write(part[1])
        finally:
            part_file.close()
        part_paths.append(part_file.name)

    if len(part_paths) == 1:
        return part_paths[0]

    # Concatenate parts without re-encoding
    list_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    with open(list_file, "w", encoding="utf-8") as f:
        for p in part_paths:
            # escape single quotes for concat list
            safe = p.replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    out_mp3 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
    subprocess.check_call([
        FFMPEG_BIN, "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        out_mp3,
    ])
    return out_mp3


def mux_video_and_audio(video_path: str, tts_mp3: str, mode: str = "mix", bg_gain: float = 0.6) -> str:
    """
    Combine video with TTS audio.
      - mode="mix": lower original audio (bg_gain) and mix TTS on top
      - mode="replace": drop original audio, keep only TTS
      - mode="dual": two audio tracks (original + TTS)
    If TTS longer than video, loop video (and its audio) and stop at the audio end.
    """
    vdur = ffprobe_duration(video_path)
    adur = ffprobe_duration(tts_mp3)
    has_a = video_has_audio(video_path)
    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name

    loop_args = ["-stream_loop", "-1"] if adur > vdur + 0.05 else []

    if mode == "replace" or not has_a:
        cmd = [
            FFMPEG_BIN, "-y", *loop_args, "-i", video_path, "-i", tts_mp3,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-shortest", out_path,
        ]
    elif mode == "dual":
        cmd = [
            FFMPEG_BIN, "-y", *loop_args, "-i", video_path, "-i", tts_mp3,
            "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-shortest", out_path,
        ]
    else:
        # mix
        fc = (
            f"[0:a]volume={bg_gain}[a0];"
            f"[1:a]volume=1.0[a1];"
            f"[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0,aresample=async=1[a]"
        )
        cmd = [
            FFMPEG_BIN, "-y", *loop_args, "-i", video_path, "-i", tts_mp3,
            "-filter_complex", fc,
            "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-shortest", out_path,
        ]

    subprocess.check_call(cmd)
    return out_path


@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "endpoint": "/process-video"})


@app.route("/process-video", methods=["POST"])
def process_video():
    try:
        upfile = next(iter(request.files.values()), None)
        if not upfile:
            return jsonify({"error": "No video file provided (form-data key 'video')"}), 400

        # Inputs
        text = (request.form.get("text") or "").strip()
        if not text:
            return jsonify({"error": "Missing 'text'"}), 400
        if len(text) > TEXT_MAX_CHARS:
            return jsonify({
                "error": f"text too long ({len(text)} chars). Limit is {TEXT_MAX_CHARS}. "
                         f"Split into multiple requests or raise TEXT_MAX_CHARS env var."
            }), 413

        voice = (request.form.get("voice") or "en-US-JennyNeural").strip()
        rate  = normalize_rate(request.form.get("rate") or "0%")
        volume = (request.form.get("volume") or "0%").strip()
        mode = (request.form.get("mode") or "mix").strip().lower()
        if mode not in {"mix", "replace", "dual"}:
            mode = "mix"
        try:
            bg = float(request.form.get("bg", "0.6"))
        except Exception:
            bg = 0.6
        bg = max(0.0, min(bg, 2.0))

        # Save upload
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            upfile.save(vf.name)
            video_path = vf.name

        # Build TTS on disk (no big buffers)
        tts_mp3 = asyncio.run(tts_to_mp3_streaming(text, voice=voice, rate=rate, volume=volume))

        # Mux
        out_path = mux_video_and_audio(video_path, tts_mp3, mode=mode, bg_gain=bg)

        return send_file(out_path, mimetype="video/mp4",
                         as_attachment=True, download_name="output.mp4")

    except MemoryError:
        return jsonify({"error": "Server out of memory. Try shorter text, smaller chunks, or upgrade instance."}), 502
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "Payload too large"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
