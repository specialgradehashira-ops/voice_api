import os, io, shlex, tempfile, subprocess
from flask import Flask, request, jsonify, send_file, after_this_request

app = Flask(__name__)

# Friendly names → espeak-ng voice codes
VOICE_MAP = {
    "en-US-JennyNeural": "en-us+f3",
    "en-US-GuyNeural": "en-us+m3",
    "en-GB-LibbyNeural": "en-gb+f3",
    "en-GB-RyanNeural": "en-gb+m3",
}
DEFAULT_ESPEAK = "en-us+f3"

def pick_voice(v: str) -> str:
    v = (v or "").strip()
    return VOICE_MAP.get(v, DEFAULT_ESPEAK)

def parse_rate(rate: str) -> int:
    base = 175  # espeak-ng default
    if not rate:
        return base
    s = rate.strip()
    if s.endswith("%"):
        try:
            pct = float(s[:-1])
        except Exception:
            pct = 0.0
        wpm = base * (1.0 + pct / 100.0)
    else:
        try:
            wpm = float(s)
        except Exception:
            wpm = base
    return max(80, min(450, int(wpm)))

def parse_volume(volume: str) -> int:
    base = 150
    if not volume:
        return base
    s = volume.strip()
    if s.endswith("%"):
        try:
            pct = float(s[:-1])
        except Exception:
            pct = 0.0
        amp = base * (1.0 + pct / 100.0)
    else:
        try:
            amp = float(s)
        except Exception:
            amp = base
    return max(0, min(200, int(amp)))

def ffprobe_duration(path: str) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]).decode().strip()
    return float(out)

def tts_espeak_to_wav(text: str, espeak_voice: str, wpm: int, amp: int) -> str:
    t = (text or " ").strip() or " "
    CH = 1800
    chunks = []
    while t:
        c = t[:CH]
        cut = c.rfind(". ")
        if cut > 600:
            c = c[:cut + 1]
        chunks.append(c)
        t = t[len(c):]

    part_paths = []
    for c in chunks:
        out_wav = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
        subprocess.check_call([
            "espeak-ng",
            "-v", espeak_voice,
            "-s", str(wpm),
            "-a", str(amp),
            "-w", out_wav,
            c
        ])
        part_paths.append(out_wav)

    if len(part_paths) == 1:
        return part_paths[0]

    list_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    with open(list_file, "w", encoding="utf-8") as f:
        for p in part_paths:
            f.write(f"file {shlex.quote(p)}\n")

    out_all = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
    subprocess.check_call([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        out_all
    ])
    return out_all

def mux_video_and_audio(video_path: str, audio_path: str) -> str:
    vdur = ffprobe_duration(video_path)
    adur = ffprobe_duration(audio_path)
    out_mp4 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name

    if adur > vdur + 0.05:
        subprocess.check_call([
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-t", f"{adur:.3f}",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            out_mp4
        ])
    else:
        subprocess.check_call([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            out_mp4
        ])
    return out_mp4

@app.route("/", methods=["GET"])
def root():
    return jsonify(ok=True, endpoint="/process-video")

@app.route("/process-video", methods=["POST"])
def process_video():
    try:
        up = next(iter(request.files.values()), None)
        if not up:
            return jsonify(error="No video file uploaded (field name 'video')"), 400

        text = (request.form.get("text") or "").strip()
        voice = request.form.get("voice", "en-US-JennyNeural")
        rate = request.form.get("rate", "+0%")
        volume = request.form.get("volume", "+0%")

        video_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
        up.save(video_path)

        espeak_voice = pick_voice(voice)
        wpm = parse_rate(rate)
        amp = parse_volume(volume)

        tts_wav = tts_espeak_to_wav(text or " ", espeak_voice, wpm, amp)
        out_mp4 = mux_video_and_audio(video_path, tts_wav)

        @after_this_request
        def _cleanup(resp):
            for p in (video_path, tts_wav):
                try:
                    os.unlink(p)
                except Exception:
                    pass
            return resp

        return send_file(out_mp4, as_attachment=True, download_name="output.mp4", mimetype="video/mp4")

    except subprocess.CalledProcessError as e:
        return jsonify(error=f"ffmpeg/espeak error: {e}"), 500
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
