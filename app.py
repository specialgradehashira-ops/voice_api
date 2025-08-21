import os
import tempfile
import subprocess
from fastapi import FastAPI, UploadFile, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import azure.cognitiveservices.speech as speechsdk

app = FastAPI()

# --- CORS (so you can call from n8n, etc) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Azure Speech Config ---
SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY")
SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")
VOICE = os.getenv("AZURE_SPEECH_VOICE", "en-US-JennyNeural")

def synthesize_speech(text: str, out_path: str, voice: str = VOICE):
    """Generate speech using Azure TTS and save to a file"""
    if not SPEECH_KEY or not SPEECH_REGION:
        raise RuntimeError("Missing Azure credentials in env vars.")

    speech_config = speechsdk.SpeechConfig(
        subscription=SPEECH_KEY, region=SPEECH_REGION
    )
    speech_config.speech_synthesis_voice_name = voice
    audio_config = speechsdk.audio.AudioOutputConfig(filename=out_path)

    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config, audio_config=audio_config
    )
    result = synthesizer.speak_text_async(text).get()

    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        raise RuntimeError(f"TTS failed: {result.reason}")

# --- Routes ---
@app.get("/")
def root():
    return {"ok": True, "endpoint": "/process-video"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/process-video")
async def process_video(
    video: UploadFile,
    text: str = Form(...),
    voice: str = Form(VOICE),
    rate: str = Form("+0%"),
    volume: str = Form("+0%"),
):
    # temp files
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "input.mp4")
        audio_path = os.path.join(tmpdir, "voice.mp3")
        out_path = os.path.join(tmpdir, "out.mp4")

        # save uploaded video
        with open(video_path, "wb") as f:
            f.write(await video.read())

        # synthesize audio
        synthesize_speech(text, audio_path, voice=voice)

        # ffmpeg: merge voiceover with video
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest", out_path,
        ]
        subprocess.run(cmd, check=True)

        return FileResponse(out_path, media_type="video/mp4", filename="out.mp4")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
