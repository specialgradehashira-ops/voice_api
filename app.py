import io, os, tempfile, asyncio, subprocess, textwrap
from flask import Flask, request, send_file, jsonify
import edge_tts
import imageio_ffmpeg

app = Flask(__name__)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
FFPROBE_BIN = imageio_ffmpeg.get_ffprobe_exe()

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

async def tts_to_mp3(text: str, voice="en-US-JennyNeural", rate="0%", volume="0%") -> str:
    # Split long text into safe ~3800-char chunks (prefer end of sentence)
    t = text.strip() or " "
    chunks = []
    CHUNK = 3800
    while t:
        c = t[:CHUNK]
        cut = c.rfind(". ")
        if cut > 1200:
            c = c[:cut+1]
        chunks.append(c)
        t = t[len(c):]

    # Produce one temp mp3 per chunk
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
        # write bytes to mp3 file
        part = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        with open(part.name, "wb") as f:
            f.write(buf.getvalue())
        part_paths.append(part.name)

    if len(part_paths) == 1:
        return part_paths[0]

    # Concatenate mp3s using ffmpeg concat demuxer (no re-encode of intermediate)
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

def mux_loop_or_trim(video_path: str, audio_path: str) -> str:
    vdur = ffprobe_duration(video_path)
    adur = ffprobe_duration(audio_path)
    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name

    if adur > vdur + 0.05:
        # Loop video until audio ends, stop at audio with -shortest
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
        # Trim video to audio length
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

        audio_mp3 = asyncio.run(tts_to_mp3(text, voice=voice, rate=rate, volume=volume))

        # Use mp3 directly (ffmpeg will transcode to AAC while muxing)
        out_path = mux_loop_or_trim(video_path, audio_mp3)

        return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="output.mp4")

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
