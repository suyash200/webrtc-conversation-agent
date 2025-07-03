from aiortc import RTCPeerConnection, events


def create_peer_connection():
    pc = RTCPeerConnection()
    # (Optional) configure STUN/TURN, media tracks, etc.
    return pc
