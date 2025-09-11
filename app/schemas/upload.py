from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional

class UploadResponse(BaseModel):
    id: int
    filename: str
    prompt: Optional[str] = None
    video_path: Optional[str] = None
    video_url: Optional[str] = None
    upload_time: datetime
    video_generated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
