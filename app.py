import io, os, tempfile, asyncio, subprocess
from flask import Flask, request, send_file, jsonify
import edge_tts
import imageio_ffmpeg

app = Flask(__name__)

# Static FFmpeg from imageio-ffmpeg (works on Render free)
FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()

VOICES_ALLOWED = {"en-US-JennyNeural", "en-US-GuyNeural", "en-GB-LibbyNeural"}

async def tts_to_mp3(text: str, voice="en-US-JennyNeural", rate="0%", volume="0%") -> str:
    """Generate MP3 from text with edge-tts, chunking long text safely."""
    t = (text or "").strip() or " "
    chunks, CHUNK = [], 3800
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
            rate=rate, volume=volume
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

    # Concatenate MP3s without re-encoding
    list_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    with open(list_file, "w", encoding="utf-8") as f:
        for p in part_paths:
            f.write(f"file '{p}'\n")

    out_mp3 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        out_mp3
    ]
    subprocess.check_call(cmd)
    return out_mp3

def mux_video_with_audio(video_path: str, audio_path: str) -> str:
    """
    Always loop the video and stop at the shortest stream.
    - If audio < video: stops at audio end (video effectively trimmed).
    - If audio > video: video loops until audio ends.
    """
    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
    cmd = [
        FFMPEG_BIN, "-y",
        "-stream_loop", "-1", "-i", video_path,   # loop video indefinitely
        "-i", audio_path,                         # audio (mp3)
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        out_path
    ]
    subprocess.check_call(cmd)
    return out_path

@app.route("/process-video", methods=["POST"])
def process_video():
    try:
        upfile = next(iter(request.files.values()), None)
        if not upfile:
            return jsonify({"error": "No video file"}), 400

        text = (request.form.get("text") or "").strip()
        voice = request.form.get("voice", "en-US-JennyNeural")
        rate = request.form.get("rate", "0%")
        volume = request.form.get("volume", "0%")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            upfile.save(vf.name)
            video_path = vf.name

        audio_mp3 = asyncio.run(tts_to_mp3(text, voice=voice, rate=rate, volume=volume))
        out_path = mux_video_with_audio(video_path, audio_mp3)

        return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="output.mp4")

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
