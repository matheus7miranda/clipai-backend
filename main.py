from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import subprocess
import os
import uuid
import shutil
import re
import base64
import imageio_ffmpeg
from openai import OpenAI

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Binario do ffmpeg embarcado via pip (nao depende do sistema)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# Cliente OpenAI (a chave vem da variavel de ambiente OPENAI_API_KEY do Railway)
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Diretorio publico onde os frames/clipes ficam acessiveis
WORK_DIR = "/tmp/clipai"
os.makedirs(WORK_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=WORK_DIR), name="files")

CLIP_SECONDS = 8

def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result

def get_duration(path):
    # Usa o proprio ffmpeg para descobrir a duracao (sem ffprobe)
    result = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
    output = result.stderr
            match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", output)
    if not match:
        raise RuntimeError("Nao foi possivel ler a duracao do video. Saida: " + output[-500:])
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)

def gerar_imagem_com_avatar(frame_path, avatar_path, output_path):
    """Fase 2: usa gpt-image-1 para recriar a cena do frame, mas com o avatar
    no lugar do personagem original. Envia o frame e o avatar como referencias."""
    prompt = (
        "Recrie esta cena exatamente igual ao primeiro frame de referencia "
        "(mesma composicao, enquadramento, cenario, iluminacao, cores e pose), "
        "mas substitua o personagem principal pela pessoa do segundo frame de "
        "referencia (o avatar). Mantenha o rosto, cabelo e caracteristicas do "
        "avatar de forma fiel e realista. Resultado fotorrealista."
    )
    frame_file = open(frame_path, "rb")
    avatar_file = open(avatar_path, "rb")
    try:
        result = openai_client.images.edit(
            model="gpt-image-1",
            image=[frame_file, avatar_file],
            prompt=prompt,
            size="1024x1024",
        )
    finally:
        frame_file.close()
        avatar_file.close()
    b64 = result.data[0].b64_json
    with open(output_path, "wb") as f:
        f.write(base64.b64decode(b64))
    return True

@app.get("/")
def root():
    return {"status": "ClipAI backend online", "version": "3.0", "ffmpeg": FFMPEG}

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
        avatar_path = None
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
            gen_name = f"gen_{index}.png"
            clip_path = os.path.join(job_dir, clip_name)
            frame_path = os.path.join(job_dir, frame_name)
            audio_path = os.path.join(job_dir, audio_name)
            gen_path = os.path.join(job_dir, gen_name)

            # Corta o clipe
            run([FFMPEG, "-y", "-ss", str(start), "-i", video_path,
                 "-t", str(dur), "-c", "copy", clip_path])
            # Extrai um frame do meio do clipe
            run([FFMPEG, "-y", "-ss", str(start + dur / 2), "-i", video_path,
                 "-frames:v", "1", "-q:v", "2", frame_path])
            # Extrai o audio do clipe
            run([FFMPEG, "-y", "-ss", str(start), "-i", video_path,
                 "-t", str(dur), "-vn", "-q:a", "2", audio_path])
            has_audio = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0

            base = f"/files/{job_id}"

            # FASE 2: gera a imagem com o avatar (se houver avatar e frame)
            generated_url = None
            gen_error = None
            if avatar_path and os.path.exists(frame_path):
                try:
                    gerar_imagem_com_avatar(frame_path, avatar_path, gen_path)
                    generated_url = f"{base}/{gen_name}"
                except Exception as ge:
                    gen_error = str(ge)

            clips.append({
                "index": index,
                "start": round(start, 2),
                "end": round(start + dur, 2),
                "duration": round(dur, 2),
                "clip_url": f"{base}/{clip_name}",
                "frame_url": f"{base}/{frame_name}",
                "audio_url": f"{base}/{audio_name}" if has_audio else None,
                "generated_url": generated_url,
                "gen_error": gen_error,
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
