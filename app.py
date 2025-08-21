import io, os, math, tempfile, asyncio
from flask import Flask, request, send_file, jsonify
import edge_tts
from pydub import AudioSegment
import subprocess
import json

app = Flask(__name__)

VOICES_ALLOWED = {"en-US-JennyNeural", "en-US-GuyNeural", "en-GB-LibbyNeural"}

def ffprobe_duration(path):
    cmd = [
        "ffprobe","-v","error","-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1", path
    ]
    out = subprocess.check_output(cmd).decode().strip()
    return float(out)

async def tts_to_wav(text, voice="en-US-JennyNeural", rate="0%", volume="0%"):
    # chunk text ~4000 chars safe chunks
    chunks = []
    t = text.strip()
    CHUNK = 3800
    while t:
        chunk = t[:CHUNK]
        cut = chunk.rfind(". ")
        if cut > 1200:
            chunk = chunk[:cut+1]
        chunks.append(chunk)
        t = t[len(chunk):]

    audio = AudioSegment.silent(duration=0)
    for chunk in chunks:
        communicate = edge_tts.Communicate(
            chunk,
            voice=voice if voice in VOICES_ALLOWED else "en-US-JennyNeural",
            rate=rate, volume=volume
        )
        wav_bytes = io.BytesIO()
        # stream to bytes
        collect = io.BytesIO()
        async for part in communicate.stream():
            if part[0] == "audio":
                collect.write(part[1])
        collect.seek(0)
        seg = AudioSegment.from_file(collect, format="mp3")  # edge-tts returns mp3 frames
        audio += seg

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    audio.export(tmp.name, format="wav")
    return tmp.name

def mux_loop_or_trim(video_path, audio_path):
    vdur = ffprobe_duration(video_path)
    adur = ffprobe_duration(audio_path)

    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
    if adur > vdur + 0.05:
        # loop video until audio finishes; stop at shortest (audio) with -shortest
        cmd = [
            "ffmpeg","-y",
            "-stream_loop","-1","-i",video_path,
            "-i", audio_path,
            "-map","0:v:0","-map","1:a:0",
            "-c:v","copy","-c:a","aac","-b:a","192k",
            "-shortest",
            out_path
        ]
    else:
        # trim video to audio length via -shortest (no loop)
        cmd = [
            "ffmpeg","-y",
            "-i", video_path,
            "-i", audio_path,
            "-map","0:v:0","-map","1:a:0",
            "-c:v","copy","-c:a","aac","-b:a","192k",
            "-shortest",
            out_path
        ]
    subprocess.check_call(cmd)
    return out_path

@app.route("/process-video", methods=["POST"])
def process_video():
    try:
        if "video" not in request.files and "file" not in request.files:
            # n8n sends binary as 'sourceVideo' param name; we accept 'video' via sendBinaryData
            # In our workflow we set binaryPropertyName=sourceVideo and body as form-data => file field name becomes 'file'
            pass
        # binary arrives under the field name used by n8n for file. Accept any key:
        upfile = next(iter(request.files.values()), None)
        if not upfile:
            return jsonify({"error": "No video file"}), 400

        text = request.form.get("text", "").strip()
        voice = request.form.get("voice", "en-US-JennyNeural")
        rate = request.form.get("rate", "0%")
        volume = request.form.get("volume", "0%")
        if not text:
            text = " "

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as vf:
            upfile.save(vf.name)
            video_path = vf.name

        # TTS (async)
        audio_path = asyncio.run(tts_to_wav(text, voice=voice, rate=rate, volume=volume))

        # mux
        out_path = mux_loop_or_trim(video_path, audio_path)

        return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="output.mp4")
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # Ensure ffmpeg is available in Render's image or add apt install in start script
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
