import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from app.logging import setup_logger

# Настройка логгера
logger = setup_logger('auth_sec', 'auth_sec.log')


load_dotenv()
# Настройки безопасности
SECRET_KEY = os.getenv('SECRET_KEY')
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
MAX_SESSIONS_PER_USER = 3

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверяет соответствие открытого пароля его хэшированной версии"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Создает хэш пароля с использованием bcrypt"""
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Создает JWT токен доступа с заданными данными и сроком действия"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    logger.debug(f"Creating token with payload: {to_encode}")
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    logger.debug(f"Created token: {encoded_jwt}")
    return encoded_jwt

def decode_token(token: str) -> dict:
    """Декодирует и проверяет JWT токен, возвращает payload"""
    try:
        logger.debug(f"Attempting to decode token: {token}")
        logger.debug(f"Using SECRET_KEY: {SECRET_KEY[:5]}...")
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        logger.debug(f"Successfully decoded token. Payload: {payload}")
        return payload
    except JWTError as e:
        logger.error(f"JWT decode error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) 