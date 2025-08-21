import os
import io
import asyncio
import tempfile
import subprocess
from typing import List
from flask import Flask, request, send_file, jsonify
import edge_tts
import imageio_ffmpeg

app = Flask(__name__)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()  # bundled ffmpeg from wheel

# Tunables (override in Render → Environment if needed)
TEXT_MAX_CHARS = int(os.environ.get("TEXT_MAX_CHARS", "20000"))
TTS_CHARS_PER_CHUNK = int(os.environ.get("TTS_CHARS_PER_CHUNK", "2200"))  # smaller = lower RAM spikes


def normalize_rate(val: str) -> str:
    """Accept '0%', '+5%', '-10%', or '15' (treated as +15%)."""
    if not val:
        return "0%"
    s = str(val).strip()
    if s.endswith("%"):
        return s
    try:
        n = int(float(s))
        return f"{n:+d}%"
    except Exception:
        return "0%"


async def tts_to_mp3_streaming(text: str, voice="en-US-JennyNeural", rate="0%", volume="0%") -> str:
    """
    Generate MP3 using Edge TTS.
    Streams bytes directly to disk (no large in-RAM buffers).
    Concats multiple parts losslessly if needed.
    """
    t = (text or " ").strip()
    # split into safe chunks
    parts: List[str] = []
    while t:
        c = t[:TTS_CHARS_PER_CHUNK]
        cut = c.rfind(". ")
        if cut > 1000:
            c = c[:cut + 1]
        parts.append(c)
        t = t[len(c):]

    out_paths: List[str] = []
    for chunk in parts:
        communicate = edge_tts.Communicate(chunk, voice=voice, rate=rate, volume=volume)
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        try:
            async for part in communicate.stream():
                if part[0] == "audio":
                    f.write(part[1])
        finally:
            f.close()
        out_paths.append(f.name)

    if len(out_paths) == 1:
        return out_paths[0]

    # concat mp3 parts without re-encoding
    list_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    with open(list_file, "w", encoding="utf-8") as lf:
        for p in out_paths:
            lf.write(f"file '{p.replace(\"'\", \"'\\\\''\")}'\n")

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
    Combine video with TTS.
      - mode="mix": quiet original (bg_gain) + TTS mixed together
      - mode="replace": drop original audio, keep only TTS
      - mode="dual": two audio tracks (original + TTS)
    Always loop video so TTS can run to completion; -shortest stops properly.
    """
    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name

    if mode == "replace":
        cmd = [
            FFMPEG_BIN, "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", tts_mp3,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-shortest", out_path,
        ]
        subprocess.check_call(cmd)
        return out_path

    if mode == "dual":
        # '0:a:0?' = map original audio only if it exists (no error if missing)
        cmd = [
            FFMPEG_BIN, "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", tts_mp3,
            "-map", "0:v:0",
            "-map", "0:a:0?", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-shortest", out_path,
        ]
        subprocess.check_call(cmd)
        return out_path

    # mode == "mix": try to mix; if video has no audio, fall back to replace
    try:
        fc = (
            f"[0:a]volume={bg_gain}[a0];"
            f"[1:a]volume=1.0[a1];"
            f"[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0,"
            f"aresample=async=1[a]"
        )
        cmd = [
            FFMPEG_BIN, "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", tts_mp3,
            "-filter_complex", fc,
            "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-shortest", out_path,
        ]
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        # No original audio? just replace.
        cmd = [
            FFMPEG_BIN, "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", tts_mp3,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
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

        text = (request.form.get("text") or "").strip()
        if not text:
            return jsonify({"error": "Missing 'text'"}), 400
        if len(text) > TEXT_MAX_CHARS:
            return jsonify({
                "error": f"text too long ({len(text)} chars). Limit is {TEXT_MAX_CHARS}. "
                         f"Split into multiple requests or lower TTS_CHARS_PER_CHUNK."
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

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            upfile.save(vf.name)
            video_path = vf.name

        tts_mp3 = asyncio.run(tts_to_mp3_streaming(text, voice=voice, rate=rate, volume=volume))
        out_path = mux_video_and_audio(video_path, tts_mp3, mode=mode, bg_gain=bg)

        return send_file(out_path, mimetype="video/mp4",
                         as_attachment=True, download_name="output.mp4")

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg error: {e}"}), 500
    except Exception as e:
        # show better error text than just "0"
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "Payload too large"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
