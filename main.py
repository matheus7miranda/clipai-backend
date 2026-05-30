from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import subprocess
import tempfile
import os
import uuid
import shutil
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Diretorio publico onde os frames/clipes ficam acessiveis
WORK_DIR = "/tmp/clipai"
os.makedirs(WORK_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=WORK_DIR), name="files")

CLIP_SECONDS = 8

def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout

def get_duration(path):
    out = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ])
    return float(out.strip())

@app.get("/")
def root():
    return {"status": "ClipAI backend online", "version": "2.0"}

@app.post("/process-video")
async def process_video(video: UploadFile = File(...), avatar: UploadFile = File(None)):
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # Salva o video enviado
        video_path = os.path.join(job_dir, "source.mp4")
        with open(video_path, "wb") as f:
            shutil.copyfileobj(video.file, f)

        # Salva o avatar se enviado
        avatar_name = None
        if avatar is not None:
            avatar_name = "avatar_" + (avatar.filename or "avatar.png")
            avatar_path = os.path.join(job_dir, avatar_name)
            with open(avatar_path, "wb") as f:
                shutil.copyfileobj(avatar.file, f)

        total = get_duration(video_path)
        clips = []
        index = 0
        start = 0.0
        while start < total:
            dur = min(CLIP_SECONDS, total - start)
            if dur < 1.0:
                break
            index += 1
            clip_name = f"clip_{index}.mp4"
            frame_name = f"frame_{index}.jpg"
            audio_name = f"audio_{index}.mp3"
            clip_path = os.path.join(job_dir, clip_name)
            frame_path = os.path.join(job_dir, frame_name)
            audio_path = os.path.join(job_dir, audio_name)

            # Corta o clipe
            run([
                "ffmpeg", "-y", "-ss", str(start), "-i", video_path,
                "-t", str(dur), "-c", "copy", clip_path
            ])
            # Extrai um frame do meio do clipe
            run([
                "ffmpeg", "-y", "-ss", str(start + dur / 2), "-i", video_path,
                "-frames:v", "1", "-q:v", "2", frame_path
            ])
            # Extrai o audio do clipe (pode falhar se nao houver audio)
            has_audio = True
            try:
                run([
                    "ffmpeg", "-y", "-ss", str(start), "-i", video_path,
                    "-t", str(dur), "-vn", "-q:a", "2", audio_path
                ])
            except Exception:
                has_audio = False

            base = f"/files/{job_id}"
            clips.append({
                "index": index,
                "start": round(start, 2),
                "end": round(start + dur, 2),
                "duration": round(dur, 2),
                "clip_url": f"{base}/{clip_name}",
                "frame_url": f"{base}/{frame_name}",
                "audio_url": f"{base}/{audio_name}" if has_audio else None,
            })
            start += CLIP_SECONDS

        return {
            "job_id": job_id,
            "total_duration": round(total, 2),
            "clip_seconds": CLIP_SECONDS,
            "num_clips": len(clips),
            "avatar": (f"/files/{job_id}/{avatar_name}" if avatar_name else None),
            "clips": clips,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
