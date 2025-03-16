from datetime import datetime
from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from transfer_data.database import get_async_session
from app.logging import setup_logger
from app.auth.models import User, UserSession
from app.auth.security import oauth2_scheme, decode_token, MAX_SESSIONS_PER_USER

# Настройка логгера
logger = setup_logger('auth_dep', 'auth_dep.log')


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_async_session)
) -> User:
    """Получает текущего пользователя по токену авторизации"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        # Удаляем префикс "Bearer " если он есть
        original_token = token
        if token.startswith("Bearer "):
            token = token[7:]
        logger.debug(f"Original token: {original_token}")
        logger.debug(f"Cleaned token: {token}")

        # Декодируем токен для получения user_id
        payload = decode_token(token)
        logger.debug(f"Token payload: {payload}")
        user_id_str: Optional[str] = payload.get("sub")
        if user_id_str is None:
            logger.error("No user_id in token payload")
            raise credentials_exception
            
        try:
            user_id = int(user_id_str)
        except ValueError:
            logger.error(f"Invalid user_id format in token: {user_id_str}")
            raise credentials_exception

        # Получаем пользователя
        user = await session.execute(
            select(User).where(User.id == user_id)
        )
        user = user.scalar_one_or_none()
        if user is None or not user.is_active:
            logger.error(f"User not found or not active: {user_id}")
            raise credentials_exception

        # Проверяем существование активной сессии для этого пользователя
        user_session = await session.execute(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.access_token == token,
                UserSession.is_active == True
            )
        )
        session_result = user_session.scalar_one_or_none()
        if not session_result:
            logger.error(f"No active session found for token. User ID: {user_id}")
            # Выводим все активные сессии пользователя для отладки
            all_sessions = await session.execute(
                select(UserSession.access_token).where(
                    UserSession.user_id == user_id,
                    UserSession.is_active == True
                )
            )
            active_tokens = [sess.access_token for sess in all_sessions.scalars().all()]
            logger.debug(f"Active tokens for user {user_id}: {active_tokens}")
            raise credentials_exception

        logger.debug(f"Found valid session: {session_result.id}")
        
        # Обновляем время последней активности
        session_result.last_activity = datetime.utcnow()
        await session.commit()
            
        logger.debug(f"Successfully authenticated user: {user.username}")
        return user
        
    except Exception as e:
        logger.error(f"Authentication error: {str(e)}")
        raise credentials_exception

async def get_current_admin_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """Проверяет, что текущий пользователь является администратором"""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions"
        )
    return current_user

async def get_client_info(request: Request) -> dict:
    """Получает информацию о клиенте (IP-адрес и User-Agent) из запроса"""
    return {
        "ip_address": request.client.host,
        "device_info": request.headers.get("user-agent", "Unknown")
    } 