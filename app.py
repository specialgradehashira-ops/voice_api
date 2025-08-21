import io, os, tempfile, asyncio, subprocess
from flask import Flask, request, send_file, jsonify
import edge_tts
from pydub import AudioSegment
import imageio_ffmpeg

app = Flask(__name__)

# ---- Use static ffmpeg/ffprobe shipped by imageio-ffmpeg ----
FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
FFPROBE_BIN = imageio_ffmpeg.get_ffprobe_exe()
AudioSegment.converter = FFMPEG_BIN
AudioSegment.ffmpeg = FFMPEG_BIN
AudioSegment.ffprobe = FFPROBE_BIN

VOICES_ALLOWED = {"en-US-JennyNeural", "en-US-GuyNeural", "en-GB-LibbyNeural"}

def ffprobe_duration(path: str) -> float:
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    out = subprocess.check_output(cmd).decode().strip()
    return float(out)

async def tts_to_wav(text, voice="en-US-JennyNeural", rate="0%", volume="0%"):
    # Chunk long text into ~3800-char pieces, prefer splitting on sentence end
    chunks, t = [], text.strip()
    CHUNK = 3800
    while t:
        chunk = t[:CHUNK]
        cut = chunk.rfind(". ")
        if cut > 1200:
            chunk = chunk[:cut + 1]
        chunks.append(chunk)
        t = t[len(chunk):]

    # Concatenate MP3 frames from edge-tts into a single AudioSegment, export as WAV
    audio = AudioSegment.silent(duration=0)
    for chunk in chunks:
        communicate = edge_tts.Communicate(
            chunk,
            voice=voice if voice in VOICES_ALLOWED else "en-US-JennyNeural",
            rate=rate, volume=volume
        )
        buf = io.BytesIO()
        async for part in communicate.stream():
            if part[0] == "audio":
                buf.write(part[1])
        buf.seek(0)
        seg = AudioSegment.from_file(buf, format="mp3")
        audio += seg

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    audio.export(tmp.name, format="wav")
    return tmp.name

def mux_loop_or_trim(video_path: str, audio_path: str) -> str:
    vdur = ffprobe_duration(video_path)
    adur = ffprobe_duration(audio_path)

    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
    if adur > vdur + 0.05:
        # Audio longer -> loop video, stop at audio end
        cmd = [
            FFMPEG_BIN, "-y",
            "-stream_loop", "-1", "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            out_path
        ]
    else:
        # Audio shorter -> trim video to audio
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", video_path,
            "-i", audio_path,
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
        # Accept whatever field name n8n sends for the file (first file wins)
        upfile = next(iter(request.files.values()), None)
        if not upfile:
            return jsonify({"error": "No video file"}), 400

        text = (request.form.get("text") or "").strip()
        voice = request.form.get("voice", "en-US-JennyNeural")
        rate = request.form.get("rate", "0%")
        volume = request.form.get("volume", "0%")
        if not text:
            text = " "

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            upfile.save(vf.name)
            video_path = vf.name

        audio_path = asyncio.run(tts_to_wav(text, voice=voice, rate=rate, volume=volume))
        out_path = mux_loop_or_trim(video_path, audio_path)

        return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="output.mp4")

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
