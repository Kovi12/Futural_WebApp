# req_types.py

from pydantic import BaseModel
from typing import List, Dict, Optional

class ServeRequest(BaseModel):
    model_name: str

class ServeResponse(BaseModel):
    service_id: str
    status: str
    message: str

class ClusterRequest(BaseModel):
    task_id: str

class ClusterResponse(BaseModel):
    cluster_id: str
    status: str
    message: str

class ServiceStatusResponse(BaseModel):
    service_id: str
    status: str
    replicas: int = 0
    message: Optional[str] = None

class ServiceEndpointResponse(BaseModel):
    service_id: str
    endpoint: str
    status: str

class QueryRequest(BaseModel):
    text: str
    model_name: Optional[str] = None
