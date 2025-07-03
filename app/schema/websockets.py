from pydantic import BaseModel, Field
from typing import Optional


class IceCandidate(BaseModel):
    foundation: str
    component_id: str
    transport: str
    priority: str
    ip: str
    port: str
    type: str
    generation: Optional[str] = None
    ufrag: Optional[str] = None
    network_cost: Optional[str] = Field(None, alias="network-cost")
    relatedAddress: Optional[str] = None
    relatedPort: Optional[str] = None
    sdpMid: Optional[str] = None
    sdpMLineIndex: Optional[int] = None
    tcpType: Optional[str] = None

    class Config:
        validate_by_name = True
