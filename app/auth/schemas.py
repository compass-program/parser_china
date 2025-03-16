from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


class UserBase(BaseModel):
    username: str


class UserCreate(UserBase):
    password: str
    is_admin: Optional[bool] = False


class UserResponse(UserBase):
    id: int
    is_active: bool
    is_admin: bool
    created_at: datetime

    class Config:
        from_attributes = True


class SessionInfo(BaseModel):
    id: int
    device_info: str
    ip_address: str
    created_at: datetime
    last_activity: datetime
    duration: str
    username: str

    class Config:
        from_attributes = True


class ActiveSessionsResponse(BaseModel):
    total_sessions: int
    sessions: List[SessionInfo]


class UserListResponse(BaseModel):
    all_users: int
    users: List[UserResponse]


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    old_token: str
