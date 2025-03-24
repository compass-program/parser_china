from datetime import datetime
from sqlalchemy import Integer, String, Column, ForeignKey, Boolean, DateTime
from sqlalchemy.orm import relationship
from transfer_data.database import Base
from app.auth.utils import get_moscow_time


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True)
    hashed_password = Column(String)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=get_moscow_time)
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")

class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    access_token = Column(String, unique=True)
    device_info = Column(String)
    ip_address = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=get_moscow_time)
    last_activity = Column(DateTime(timezone=True), default=get_moscow_time)

    user = relationship("User", back_populates="sessions")