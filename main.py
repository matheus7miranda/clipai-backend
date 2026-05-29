from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import openai
import jwt
import time
import httpx
import os

app = FastAPI()

app.add_middleware(
      CORSMiddleware,
      allow_origins=["*"],
      allow_methods=["*"],
      allow_headers=["*"],
)

openai.api_key = os.environ.get("OPENAI_API_KEY")
KLING_ACCESS_KEY = os.environ.get("KLING_ACCESS_KEY")
KLING_SECRET_KEY = os.environ.get("KLING_SECRET_KEY")

def generate_kling_token():
      payload = {
                "iss": KLING_ACCESS_KEY,
                "exp": int(time.time()) + 1800,
                "nbf": int(time.time()) - 5
      }
      return jwt.encode(payload, KLING_SECRET_KEY, algorithm="HS256")

class ScriptRequest(BaseModel):
      topic: str
      duration: int = 30

class VideoRequest(BaseModel):
      prompt: str
      duration: int = 5
      aspect_ratio: str = "16:9"

class TaskRequest(BaseModel):
      task_id: str

@app.post("/api/generate-script")
async def generate_script(req: ScriptRequest):
      try:
                response = openai.chat.completions.create(
                              model="gpt-4o",
                              messages=[
                                                {"role": "system", "content": "Voce e um roteirista criativo especialista em videos virais curtos."},
                                                {"role": "user", "content": f"Crie um roteiro de {req.duration} segundos sobre: {req.topic}. Seja direto, impactante e envolvente."}
                              ]
                )
                return {"script": response.choices[0].message.content}
except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/generate-video")
async def generate_video(req: VideoRequest):
      token = generate_kling_token()
      headers = {
          "Authorization": f"Bearer {token}",
          "Content-Type": "application/json"
      }
      payload = {
          "model_name": "kling-v1",
          "prompt": req.prompt,
          "duration": str(req.duration),
          "aspect_ratio": req.aspect_ratio
      }
      async with httpx.AsyncClient() as client:
                response = await client.post(
                              "https://api.klingai.com/v1/videos/text2video",
                              json=payload,
                              headers=headers,
                              timeout=30
                )
            if response.status_code != 200:
                      raise HTTPException(status_code=response.status_code, detail=response.text)
                  data = response.json()
    return {"task_id": data["data"]["task_id"]}

@app.post("/api/video-status")
async def video_status(req: TaskRequest):
      token = generate_kling_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
              response = await client.get(
                            f"https://api.klingai.com/v1/videos/text2video/{req.task_id}",
                            headers=headers,
                            timeout=30
              )
          if response.status_code != 200:
                    raise HTTPException(status_code=response.status_code, detail=response.text)
    data = response.json()
    task = data["data"]
    status = task["task_status"]
    result = {"status": status, "task_id": req.task_id}
    if status == "succeed":
              result["video_url"] = task["task_result"]["videos"][0]["url"]
          return result

@app.get("/")
def root():
      return {"status": "ClipAI rodando!"}
