from aiortc import RTCSessionDescription, RTCPeerConnection, RTCIceCandidate
from fastapi import WebSocket
from app.schema import IceCandidate


async def handle_signaling_offer(
    peer_conn: RTCPeerConnection, websocket: WebSocket, message: dict, session_id: str
):
    try:
        offer_sdp = message.get("sdp")

        if not offer_sdp:
            return ValueError("No SPD offer provided")

        print(f"Processing SDP offer for session {session_id}")

        # setting description for peer connection

        offer = RTCSessionDescription(sdp=offer_sdp, type="offer")

        await peer_conn.setRemoteDescription(offer)
        # Create answer
        answer = await peer_conn.createAnswer()

        # Set local description (our answer)
        await peer_conn.setLocalDescription(answer)

        await websocket.send_json(
            {
                "type": "answer",
                "sdp": peer_conn.localDescription.sdp,
                "session_id": session_id,
            }
        )
        print(f"SDP answer sent to session {session_id}")

    except Exception as e:
        print(f"Error handling offer for session {session_id}: {e}")
        await websocket.send_json(
            {
                "type": "error",
                "message": f"Failed to process offer: {str(e)}",
                "session_id": session_id,
            }
        )


async def handle_answer(peer_conn: RTCPeerConnection, message: dict, session_id: str):
    """Handle SDP answer from client"""
    try:
        answer_sdp = message.get("sdp")
        if not answer_sdp:
            raise ValueError("No SDP in answer message")

        print(f"Processing SDP answer for session {session_id}")

        # Import aiortc classes
        from aiortc import RTCSessionDescription

        # Create RTCSessionDescription object for the answer
        answer = RTCSessionDescription(sdp=answer_sdp, type="answer")

        # Set remote description (client's answer)
        await peer_conn.setRemoteDescription(answer)

        print(f"SDP answer processed for session {session_id}")

    except Exception as e:
        print(f"Error handling answer for session {session_id}: {e}")


def parse_ice_candidate(candidate_str) -> IceCandidate:
    if candidate_str.startswith("candidate:"):
        candidate_str = candidate_str[len("candidate:") :]

    parts = candidate_str.split()

    if len(parts) < 8:
        raise ValueError("Invalid ICE candidate format")

    # Initial required fields
    candidate_info = {
        "foundation": parts[0],
        "component_id": parts[1],
        "transport": parts[2],
        "priority": parts[3],
        "ip": parts[4],
        "port": parts[5],
        "type": parts[7],  # index 6 is "typ"
    }

    # Optional fields
    i = 8
    while i < len(parts):
        key = parts[i]
        value = parts[i + 1] if i + 1 < len(parts) else None

        # Normalize key to match Pydantic field names
        if key == "network-cost":
            candidate_info["network_cost"] = value
        elif key == "tcpType":
            candidate_info["tcpType"] = value
        elif key == "raddr":
            candidate_info["relatedAddress"] = value
        elif key == "rport":
            candidate_info["relatedPort"] = value
        elif key == "generation":
            candidate_info["generation"] = value
        elif key == "ufrag":
            candidate_info["ufrag"] = value
        elif key == "sdpMid":
            candidate_info["sdpMid"] = value
        elif key == "sdpMLineIndex":
            candidate_info["sdpMLineIndex"] = int(value) if value is not None else None
        else:
            candidate_info[key] = value  # catch-all fallback

        i += 2

    return IceCandidate(**candidate_info)


async def handle_ice_candidate(
    peer_conn: RTCPeerConnection, web_socket: WebSocket, message: dict, session_id: str
):
    try:
        candidate_data = message.get("candidate")
        if not candidate_data:
            print(f"Received end-of-candidates signal for session {session_id}")
            return
        cs = message.get("candidate")
        ice_candidate = parse_ice_candidate(cs.get("candidate"))

        rtc_candidate = RTCIceCandidate(
            component=int(ice_candidate.component_id),
            foundation=ice_candidate.foundation,
            ip=ice_candidate.ip,
            protocol=ice_candidate.transport,  # fixed typo: 'porocol' → 'transport'
            type=ice_candidate.type,
            priority=int(ice_candidate.priority),
            port=int(ice_candidate.port),  # port was missing
            relatedAddress=getattr(ice_candidate, "relatedAddress", None),
            # relatedPort=int(getattr(ice_candidate, "relatedPort", 0)) or None,
            sdpMid=cs.get("sdpMID"),
            sdpMLineIndex=cs.get("sdpMLineIndex"),
            tcpType=(
                getattr(ice_candidate, "tcpType", None)
                if ice_candidate.transport == "tcp"
                else None
            ),
        )

        await peer_conn.addIceCandidate(candidate=rtc_candidate)

        print(f"Adding ICE candidate for session {session_id}")
    except Exception as e:
        print(e)
