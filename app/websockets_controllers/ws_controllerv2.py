import json
import asyncio
from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict, Optional
from aiortc import AudioStreamTrack, RTCDataChannel, events
from fastapi import APIRouter
import time

from app.util.peer_connection_util import create_peer_connection
from app.util import handle_signaling_offer, handle_answer, handle_ice_candidate
from .audio_controller import AudioProcessor

ws_router_v2 = APIRouter(prefix="/ws")


active_sessions: Dict[str, dict] = {}


@ws_router_v2.websocket("/signaling/{session_id}")
async def signaling(websocket: WebSocket, session_id: str):
    global active_sessions
    if not session_id:
        await websocket.close(code=4000, reason="No session_id provided")
        return

    await websocket.accept()

    msg_type = websocket.get("type")

    print(f"WebRTC signaling connected for session: {session_id}")

    # Create peer connection
    peer_conn = create_peer_connection()

    # create an active session count
    active_sessions[session_id] = {
        "websocket": websocket,
        "peer_connection": peer_conn,
        "audio_service": None,
        "status": "connecting",
        "last_activity": time.time(),
    }

    # print(await peer_conn.getStats())
    # z = websocket.iter_json()
    # print(z)

    # check the msg type for offer

    try:
        async for data in websocket.iter_text():
            message = json.loads(data)
            msg_type = message.get("type")

            @peer_conn.on("track")
            async def on_track(track: AudioStreamTrack):
                print(f"Track received: kind={track.kind}", track)
                if track.kind == "audio":
                    print("Audio track received")
                    frame = await track.recv()
                    print(frame.dts)

            @peer_conn.on("datachannel")
            def on_data_channel_setup(channel: RTCDataChannel):
                print(
                    f"DataChannel created: {channel.label}, readyState: {channel.readyState}",
                    # print(channel),
                )
                active_sessions[session_id]["audio_service"] = AudioProcessor(
                    session_id=session_id, channel=channel
                )

                @channel.on("open")
                def on_data_channel_data(event):
                    print("channel received data", event)

                @channel.on("message")
                async def on_message(message):
                    if not isinstance(message, bytes):
                        print(f"Received via DataChannel: {message}")
                    else:
                        ac = active_sessions[session_id]["audio_service"]
                        response_audio = await ac.add_audio_chunk(message)
                        if response_audio:
                            try:
                                channel.send(response_audio)
                            except RuntimeError:
                                print(RuntimeError)

                # res = await audio_processor.add_audio_chunk(message)

                # audio_processor = AudioProcessor(session_id, channel=channel)
                # print(len(message))

                # channel.send("Server received: ")

                @channel.on("data")
                async def data(event):
                    print("the event on the channel is ", event)

            @peer_conn.on("icecandidate")
            def on_ice_candidate(event):
                candidate = event.candidate
                print(f"ICE candidate generated for session {session_id}")

                if candidate is not None:
                    try:
                        # Schedule the coroutine to run in the event loop
                        asyncio.create_task(
                            websocket.send_json(
                                {
                                    "type": "ice",
                                    "candidate": {
                                        "candidate": candidate.candidate,
                                        "sdpMid": candidate.sdpMid,
                                        "sdpMLineIndex": candidate.sdpMLineIndex,
                                    },
                                    "session_id": session_id,
                                }
                            )
                        )
                        print(f"ICE candidate sent to session {session_id}")
                    except Exception as e:
                        print(f"Failed to send ICE candidate to {session_id}: {e}")

            @peer_conn.on("connectionstatechange")
            def on_connection_state_change():
                state = peer_conn.connectionState
                print(f"Session {session_id} connection state: {state}")

                if session_id in active_sessions:
                    active_sessions[session_id]["status"] = state
                    active_sessions[session_id]["last_activity"] = time.time()

                # Schedule the coroutine to run in the event loop
                try:
                    asyncio.create_task(
                        websocket.send_json(
                            {
                                "type": "connection_state",
                                "state": state,
                                "session_id": session_id,
                            }
                        )
                    )
                except Exception as e:
                    print(f"Failed to send connection state to {session_id}: {e}")

            if msg_type == "offer":
                await handle_signaling_offer(
                    peer_conn=peer_conn,
                    websocket=websocket,
                    message=message,
                    session_id=session_id,
                )
            elif msg_type == "answer":
                await handle_answer(
                    peer_conn=peer_conn, message=json.loads(data), session_id=session_id
                )
            elif msg_type == "ice":
                await handle_ice_candidate(
                    peer_conn=peer_conn,
                    web_socket=websocket,
                    message=json.loads(data),
                    session_id=session_id,
                )
            elif msg_type == "ping":
                # Handle keepalive
                await websocket.send_json({"type": "pong", "session_id": session_id})
    except Exception as e:
        print(e)
