from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import subprocess
import os
import uuid
import shutil
import re
import json
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
    return subprocess.run(cmd, capture_output=True, text=True)

def get_duration(path):
    # Usa o proprio ffmpeg para descobrir a duracao (sem ffprobe)
    result = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
    output = result.stderr
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", output)
    if not match:
        raise RuntimeError("Nao foi possivel ler a duracao do video. Saida: " + output[-500:])
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)

def img_to_data_url(path):
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return "data:image/jpeg;base64," + b64

def analisar_frame(frame_path):
    """Usa o GPT-4o (visao) para contar pessoas e descrever a cena do frame.
    Retorna dict: {num_pessoas: int, descricao: str}."""
    data_url = img_to_data_url(frame_path)
    instrucao = (
        "Analise esta imagem de uma cena de video. Responda APENAS com um JSON "
        "valido no formato {\"num_pessoas\": <numero inteiro de pessoas visiveis>, "
        "\"descricao\": \"<descricao curta da cena em portugues, cenario, acao e "
        "aparencia das pessoas>\"}. Nao escreva mais nada alem do JSON."
    )
    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": instrucao},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        max_tokens=300,
    )
    texto = resp.choices[0].message.content.strip()
    # Remove cercas de codigo se vierem
    texto = re.sub(r"^```(json)?|```$", "", texto.strip()).strip()
    try:
        dados = json.loads(texto)
        return {
            "num_pessoas": int(dados.get("num_pessoas", 0)),
            "descricao": str(dados.get("descricao", "")),
        }
    except Exception:
        return {"num_pessoas": 0, "descricao": texto}

@app.get("/")
def root():
    return {"status": "ClipAI backend online", "version": "4.0", "ffmpeg": FFMPEG}

@app.post("/process-video")
async def process_video(video: UploadFile = File(...), avatar: UploadFile = File(None)):
    """Fase 1+: corta o video em cenas, extrai frame e audio, e para cada cena
    analisa quantas pessoas tem e gera uma descricao. NAO gera mais a imagem
    automaticamente - isso agora e feito sob demanda em /gerar-imagem."""
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        video_path = os.path.join(job_dir, "source.mp4")
        with open(video_path, "wb") as f:
            shutil.copyfileobj(video.file, f)

        avatar_name = None
        if avatar is not None:
            avatar_name = "avatar_" + (avatar.filename or "avatar.png")
            with open(os.path.join(job_dir, avatar_name), "wb") as f:
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

            run([FFMPEG, "-y", "-ss", str(start), "-i", video_path,
                 "-t", str(dur), "-c", "copy", clip_path])
            run([FFMPEG, "-y", "-ss", str(start + dur / 2), "-i", video_path,
                 "-frames:v", "1", "-q:v", "2", frame_path])
            run([FFMPEG, "-y", "-ss", str(start), "-i", video_path,
                 "-t", str(dur), "-vn", "-q:a", "2", audio_path])
            has_audio = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0

            # Analisa o frame: numero de pessoas + descricao
            num_pessoas = 0
            descricao = ""
            analise_erro = None
            try:
                info = analisar_frame(frame_path)
                num_pessoas = info["num_pessoas"]
                descricao = info["descricao"]
            except Exception as ae:
                analise_erro = str(ae)

            base = f"/files/{job_id}"
            clips.append({
                "index": index,
                "start": round(start, 2),
                "end": round(start + dur, 2),
                "duration": round(dur, 2),
                "clip_url": f"{base}/{clip_name}",
                "frame_url": f"{base}/{frame_name}",
                "audio_url": f"{base}/{audio_name}" if has_audio else None,
                "num_pessoas": num_pessoas,
                "descricao": descricao,
                "analise_erro": analise_erro,
                "generated_url": None,
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

@app.post("/gerar-imagem")
async def gerar_imagem(
    frame: UploadFile = File(...),
    usar_avatar: str = Form("false"),
    avatar: UploadFile = File(None),
    referencia: UploadFile = File(None),
    descricao: str = Form(""),
):
    """Fase 2 sob demanda: gera UMA imagem a partir do frame da cena.
    - usar_avatar = "true"/"false": se deve inserir o avatar no lugar do personagem.
    - avatar: foto do avatar (usada se usar_avatar = true).
    - referencia: imagem de referencia opcional enviada pelo usuario para essa cena.
    - descricao: texto da cena para reforcar o prompt."""
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, "gen", job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        frame_path = os.path.join(job_dir, "frame.jpg")
        with open(frame_path, "wb") as f:
            shutil.copyfileobj(frame.file, f)

        imagens = [open(frame_path, "rb")]

        usar = str(usar_avatar).lower() in ("true", "1", "sim", "yes")
        if usar and avatar is not None:
            avatar_path = os.path.join(job_dir, "avatar.png")
            with open(avatar_path, "wb") as f:
                shutil.copyfileobj(avatar.file, f)
            imagens.append(open(avatar_path, "rb"))

        if referencia is not None:
            ref_path = os.path.join(job_dir, "ref.png")
            with open(ref_path, "wb") as f:
                shutil.copyfileobj(referencia.file, f)
            imagens.append(open(ref_path, "rb"))

        if usar and avatar is not None:
            prompt = (
                "Recrie esta cena igual ao primeiro frame de referencia (mesma "
                "composicao, enquadramento, cenario, iluminacao e pose), mas "
                "substitua o personagem principal pela pessoa do segundo frame "
                "(o avatar), mantendo o rosto fiel. Resultado fotorrealista."
            )
        else:
            prompt = (
                "Recrie esta cena igual ao frame de referencia (mesma composicao, "
                "enquadramento, cenario, iluminacao, pose e personagens). "
                "Resultado fotorrealista de alta qualidade."
            )
        if descricao:
            prompt += " Contexto da cena: " + descricao

        try:
            result = openai_client.images.edit(
                model="gpt-image-1",
                image=imagens,
                prompt=prompt,
                size="1024x1024",
            )
        finally:
            for f in imagens:
                f.close()

        b64 = result.data[0].b64_json
        out_name = "gen.png"
        out_path = os.path.join(job_dir, out_name)
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(b64))

        return {"generated_url": f"/files/gen/{job_id}/{out_name}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
