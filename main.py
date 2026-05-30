from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import openai
import httpx
import jwt
import time
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
KLING_ACCESS_KEY = os.environ.get('KLING_ACCESS_KEY')
KLING_SECRET_KEY = os.environ.get('KLING_SECRET_KEY')

openai.api_key = OPENAI_API_KEY


class ScriptRequest(BaseModel):
    topic: str
    style: str = "cinematic"
    duration: int = 30


class VideoRequest(BaseModel):
    prompt: str
    duration: int = 5


def generate_kling_token():
    payload = {
        "iss": KLING_ACCESS_KEY,
        "exp": int(time.time()) + 1800,
        "nbf": int(time.time()) - 5
    }
    token = jwt.encode(payload, KLING_SECRET_KEY, algorithm="HS256")
    return token


@app.get("/")
def root():
    return {"status": "ClipAI backend online"}


@app.post("/generate-script")
async def generate_script(request: ScriptRequest):
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional video script writer. Create engaging, concise scripts for short videos."
                },
                {
                    "role": "user",
                    "content": f"Write a {request.duration}-second {request.style} video script about: {request.topic}. Format it as a list of scenes with descriptions."
                }
            ]
        )
        script = response.choices[0].message.content
        return {"script": script, "topic": request.topic}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate-video")
async def generate_video(request: VideoRequest):
    try:
        token = generate_kling_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "model_name": "kling-v1",
            "prompt": request.prompt,
            "mode": "std",
            "duration": str(request.duration)
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.klingai.com/v1/videos/text2video",
                json=payload,
                headers=headers
            )
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=response.text)
            data = response.json()
            task_id = data.get("data", {}).get("task_id")
            return {"task_id": task_id, "status": "processing"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/video-status/{task_id}")
async def get_video_status(task_id: str):
    try:
        token = generate_kling_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"https://api.klingai.com/v1/videos/text2video/{task_id}",
                headers=headers
            )
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=response.text)
            data = response.json()
            task_status = data.get("data", {}).get("task_status")
            videos = data.get("data", {}).get("task_result", {}).get("videos", [])
            video_url = videos[0].get("url") if videos else None
            return {
                "task_id": task_id,
                "status": task_status,
                "video_url": video_url
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
