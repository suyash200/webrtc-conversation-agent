from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from fastapi.responses import HTMLResponse
import os

from app.websockets_controllers import ws_router_v2, ws_router_audio


load_dotenv()
app = FastAPI()


@app.get("/")
async def root():
    return {"message": "Hello World"}


app.include_router(ws_router_v2)
app.include_router(ws_router_audio)


# @app.websocket("/signal")
# async def websocket_endpoint(websocket: WebSocket):
#     await websocket.accept()
#     print(f"Signaling session: qb")
#     await websocket.send_text(f"Signaling started for abc")
#     await websocket.close()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Sec-WebSocket-Protocol"],
)


@app.get("/static")
async def get_html():
    file_path = os.path.join(
        "static", "/home/suyash/Eresha/real-time-conversation-service/app/ui.html"
    )
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>File not found</h1>", status_code=404)
