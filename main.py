from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import subprocess
import os
import uuid
import shutil
import json
import re
import base64
from openai import OpenAI
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
WORKDIR = os.path.join(os.getcwd(), 'work')
os.makedirs(WORKDIR, exist_ok=True)
CLIP_SECONDS = 8

openai_client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)
app.mount('/files', StaticFiles(directory=WORKDIR), name='files')


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def get_duration(path):
    out = subprocess.run([FFMPEG, '-i', path], capture_output=True, text=True).stderr
    m = re.search(r'Duration: (\d+):(\d+):(\d+\.\d+)', out)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def _img_to_data_url(path):
    with open(path, 'rb') as f:
        b = f.read()
    return 'data:image/jpeg;base64,' + base64.b64encode(b).decode()


def _strip_json(texto):
    texto = texto.strip()
    texto = re.sub(r'^```(json)?', '', texto).strip()
    texto = re.sub(r'```$', '', texto).strip()
    return texto


def analisar_frame(frame_path):
    data_url = _img_to_data_url(frame_path)
    instrucoes = (
        'Voce e um diretor de fotografia. Analise este frame de video e responda '
        'APENAS um JSON valido, sem texto extra, no formato: '
        '{"num_pessoas": <inteiro>, "descricao": "<descricao rica e detalhada da '
        'cena em portugues: cenario, ambiente, iluminacao, enquadramento, a acao e a '
        'aparencia/roupa/pose das pessoas se houver. Se nao houver pessoas, descreva o '
        'cenario e os objetos mesmo assim. Minimo 30 palavras.>"}.'
    )
    resp = openai_client.chat.completions.create(
        model='gpt-4o',
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': instrucoes},
                {'type': 'image_url', 'image_url': {'url': data_url}},
            ],
        }],
        max_tokens=400,
    )
    texto = _strip_json(resp.choices[0].message.content)
    try:
        dados = json.loads(texto)
        desc = str(dados.get('descricao', '')).strip()
        if not desc:
            desc = 'Cena de video sem descricao detectada.'
        return {'num_pessoas': int(dados.get('num_pessoas', 0)), 'descricao': desc}
    except Exception:
        return {'num_pessoas': 0, 'descricao': texto or 'Cena de video.'}


def descrever_avatar(avatar_path):
    data_url = _img_to_data_url(avatar_path)
    instrucoes = (
        'Descreva em detalhe APENAS a aparencia fisica desta pessoa para que um gerador '
        'de imagens recrie o mesmo rosto: genero aparente, faixa etaria, tom de pele, '
        'formato do rosto, cor e estilo do cabelo, cor dos olhos, barba/bigode, oculos e '
        'tracos marcantes. Responda em uma frase corrida em portugues.'
    )
    resp = openai_client.chat.completions.create(
        model='gpt-4o',
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': instrucoes},
                {'type': 'image_url', 'image_url': {'url': data_url}},
            ],
        }],
        max_tokens=200,
    )
    return resp.choices[0].message.content.strip()


@app.get('/')
def root():
    return {'status': 'ClipAI backend online', 'version': '5.0', 'ffmpeg': FFMPEG}


@app.post('/process-video')
async def process_video(video: UploadFile = File(...), avatar: UploadFile = File(None)):
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORKDIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        video_path = os.path.join(job_dir, 'source.mp4')
        with open(video_path, 'wb') as f:
            shutil.copyfileobj(video.file, f)

        avatar_name = None
        if avatar is not None:
            avatar_name = 'avatar_' + (avatar.filename or 'avatar.png')
            with open(os.path.join(job_dir, avatar_name), 'wb') as f:
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
            clip_name = f'clip_{index}.mp4'
            frame_name = f'frame_{index}.jpg'
            audio_name = f'audio_{index}.mp3'
            clip_path = os.path.join(job_dir, clip_name)
            frame_path = os.path.join(job_dir, frame_name)
            audio_path = os.path.join(job_dir, audio_name)

            run([FFMPEG, '-y', '-ss', str(start), '-i', video_path,
                 '-t', str(dur), '-c', 'copy', clip_path])
            run([FFMPEG, '-y', '-ss', str(start + dur / 2), '-i', video_path,
                 '-frames:v', '1', '-q:v', '2', frame_path])
            run([FFMPEG, '-y', '-ss', str(start), '-i', video_path,
                 '-t', str(dur), '-vn', '-q:a', '2', audio_path])
            has_audio = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0

            num_pessoas = 0
            descricao = ''
            analise_erro = None
            try:
                info = analisar_frame(frame_path)
                num_pessoas = info['num_pessoas']
                descricao = info['descricao']
            except Exception as ae:
                analise_erro = str(ae)

            base = f'/files/{job_id}'
            clips.append({
                'index': index,
                'start': round(start, 2),
                'end': round(start + dur, 2),
                'duration': round(dur, 2),
                'clip_url': f'{base}/{clip_name}',
                'frame_url': f'{base}/{frame_name}',
                'audio_url': f'{base}/{audio_name}' if has_audio else None,
                'num_pessoas': num_pessoas,
                'descricao': descricao,
                'analise_erro': analise_erro,
                'generated_url': None,
            })
            start += CLIP_SECONDS

        return {
            'job_id': job_id,
            'total_duration': round(total, 2),
            'clip_seconds': CLIP_SECONDS,
            'num_clips': len(clips),
            'avatar': (f'/files/{job_id}/{avatar_name}' if avatar_name else None),
            'clips': clips,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/gerar-imagem')
async def gerar_imagem(
    frame: UploadFile = File(...),
    usar_avatar: str = Form('false'),
    avatar: UploadFile = File(None),
    referencia: UploadFile = File(None),
    descricao: str = Form(''),
):
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORKDIR, 'gen', job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        frame_path = os.path.join(job_dir, 'frame.jpg')
        with open(frame_path, 'wb') as f:
            shutil.copyfileobj(frame.file, f)

        usar = str(usar_avatar).lower() in ('true', '1', 'sim', 'yes')

        cena = (descricao or '').strip()
        try:
            cena_info = analisar_frame(frame_path)
            if cena_info.get('descricao'):
                cena = cena_info['descricao']
        except Exception:
            pass
        if not cena:
            cena = 'Cena de video realista.'

        avatar_desc = ''
        if usar and avatar is not None:
            avatar_path = os.path.join(job_dir, 'avatar.png')
            with open(avatar_path, 'wb') as f:
                shutil.copyfileobj(avatar.file, f)
            try:
                avatar_desc = descrever_avatar(avatar_path)
            except Exception:
                avatar_desc = ''

        prompt = (
            'Fotografia ultra realista, qualidade de cinema, pessoa humana real, '
            'pele e rosto naturais e nitidos, sem deformacoes, iluminacao realista. '
            'Recrie a seguinte cena: ' + cena
        )
        if usar and avatar_desc:
            prompt += (
                ' O personagem principal deve ser exatamente esta pessoa: ' + avatar_desc +
                ' Mantenha o rosto e a identidade dessa pessoa fieis e reconheciveis.'
            )
        prompt += ' Resultado fotorealista, humano, alta definicao, sem artefatos.'

        result = openai_client.images.generate(
            model='gpt-image-1',
            prompt=prompt[:3900],
            size='1024x1024',
            quality='high',
        )

        b64 = result.data[0].b64_json
        out_name = 'gen.png'
        out_path = os.path.join(job_dir, out_name)
        with open(out_path, 'wb') as f:
            f.write(base64.b64decode(b64))

        return {
            'generated_url': f'/files/gen/{job_id}/{out_name}',
            'prompt': prompt,
            'descricao': cena,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
