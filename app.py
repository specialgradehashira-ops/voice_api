import os
import tempfile
import subprocess
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# ---- helpers ---------------------------------------------------------------

def parse_pct(s: str, default: int = 0) -> int:
    """
    Accepts strings like '+0%', '-10%', '15', '0%'.
    Returns an int percent (no % sign). Falls back to default on junk.
    """
    if s is None:
        return default
    s = str(s).strip()
    if not s:
        return default
    if s.endswith("%"):
        s = s[:-1]
    try:
        return int(s)
    except Exception:
        return default


def map_voice_to_espeak(v: str) -> str:
    """
    Map common MS voice names to espeak-ng voices.
    We keep it simple: en-US -> en-us, en-GB -> en-uk, else en.
    """
    if not v:
        return "en-us"
    m = v.lower()
    if "en-us" in m or "jenny" in m or "guy" in m:
        return "en-us"
    if "en-gb" in m or "libby" in m or "uk" in m:
        return "en-uk"
    if "en" in m:
        return "en"
    return "en-us"


def media_duration(path: str) -> float:
    """
    Use ffprobe to get media duration (in seconds, float).
    """
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    out = subprocess.check_output(cmd).decode().strip()
    return float(out or 0.0)


def tts_espeak_to_mp3(text: str, voice: str, rate_pct: int, vol_pct: int) -> str:
    """
    Use espeak-ng to synthesize WAV, then convert to MP3 with ffmpeg.
    - voice: espeak-ng voice id (e.g. 'en-us', 'en-uk', 'en')
    - rate_pct: -90..+200 (roughly). Base WPM = 175.
    - vol_pct:  -100..+100. Base amplitude = 100 (range 0..200).
    Returns path to mp3 file.
    """
    # base settings
    base_wpm = 175
    base_amp = 100

    wpm = int(base_wpm * (1.0 + rate_pct / 100.0))
    wpm = max(80, min(300, wpm))           # clamp

    amp = int(base_amp * (1.0 + vol_pct / 100.0))
    amp = max(0, min(200, amp))            # clamp

    wav_path = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
    mp3_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
    txt_path = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name

    # write text to file (so we don't fight with shell quoting limits)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text if text.strip() else " ")

    # espeak-ng -> wav
    subprocess.check_call([
        "espeak-ng",
        "-v", voice,
        "-s", str(wpm),
        "-a", str(amp),
        "-w", wav_path,
        "-f", txt_path,
    ])

    # wav -> mp3
    subprocess.check_call([
        FFMPEG, "-y",
        "-i", wav_path,
        "-codec:a", "libmp3lame", "-b:a", "192k",
        mp3_path
    ])

    # cleanup text + wav (keep mp3)
    try:
        os.remove(txt_path)
    except Exception:
        pass
    try:
        os.remove(wav_path)
    except Exception:
        pass

    return mp3_path


def mux_video_and_audio(video_path: str, audio_path: str) -> str:
    """
    If audio is longer than the video, loop the video until audio ends.
    Otherwise, trim to the shortest (keeps video length if audio shorter).
    Returns output .mp4 path.
    """
    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name

    vdur = media_duration(video_path)
    adur = media_duration(audio_path)

    if adur > vdur + 0.05:
        # loop video, stop at audio end
        cmd = [
            FFMPEG, "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            out_path
        ]
    else:
        # just pair them, cut to shortest
        cmd = [
            FFMPEG, "-y",
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

# ---- routes ----------------------------------------------------------------

@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "endpoint": "/process-video"})

@app.route("/process-video", methods=["POST"])
def process_video():
    """
    Multipart form-data:
      - video  (File) : required .mp4 (or any ffmpeg-readable video)
      - text   (Text) : required
      - voice  (Text) : optional (examples: en-US-JennyNeural, en-US, en-GB)
      - rate   (Text) : optional percent string like '+0%', '-10%', '15%'
      - volume (Text) : optional percent string like '+0%', '-10%', '15%'
    Returns the final MP4 as a file download.
    """
    try:
        up = next(iter(request.files.values()), None)
        if not up:
            return jsonify({"error": "Missing 'video' file"}), 400

        text   = (request.form.get("text") or "").strip()
        voice  = request.form.get("voice", "en-US")
        rate   = request.form.get("rate", "+0%")
        volume = request.form.get("volume", "+0%")

        if not text:
            return jsonify({"error": "Missing 'text'"}), 400

        # save upload
        video_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
        up.save(video_path)

        # normalize voice/rate/volume for espeak-ng
        espeak_voice = map_voice_to_espeak(voice)
        rate_pct = parse_pct(rate, 0)
        vol_pct  = parse_pct(volume, 0)

        # synthesize
        mp3_path = tts_espeak_to_mp3(text, espeak_voice, rate_pct, vol_pct)

        # mux
        out_path = mux_video_and_audio(video_path, mp3_path)

        # cleanup
        try:
            os.remove(mp3_path)
        except Exception:
            pass
        try:
            os.remove(video_path)
        except Exception:
            pass

        return send_file(
            out_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="output.mp4"
        )

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg/espeak error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Render sets PORT; default to 10000 for local
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
