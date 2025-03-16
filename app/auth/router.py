from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from transfer_data.database import get_async_session
from app.auth.models import User, UserSession
from app.auth.schemas import (
    UserCreate, 
    UserResponse, 
    Token, 
    SessionInfo, 
    ActiveSessionsResponse, 
    UserListResponse, 
    TokenRefresh,
)
from app.auth.security import (
    verify_password, 
    get_password_hash, 
    create_access_token, 
    ACCESS_TOKEN_EXPIRE_MINUTES,
    MAX_SESSIONS_PER_USER
)
from app.auth.dependencies import get_current_user, get_current_admin_user, get_client_info, decode_token
from app.logging import setup_logger

# Настройка логгера
logger = setup_logger('auth_router', 'auth_router.log')


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token", response_model=Token)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_async_session),
    client_info: dict = Depends(get_client_info)
):
    """
    Аутентификация пользователя (OAuth2 password flow)
    
    Параметры form_data:
    Все параметры передаются в виде формы (application/x-www-form-urlencoded):
    - username: имя пользователя (обязательное)
    - password: пароль (обязательное)
    - grant_type: тип авторизации (автоматически "password")
    - scope: области доступа (не используется)
    - client_id: идентификатор клиента (не используется)
    - client_secret: секрет клиента (не используется)
    
    Returns:
        Token: Объект с токеном доступа
            - access_token: JWT токен для доступа к API
            - token_type: тип токена (всегда "bearer")
    
    Raises:
        HTTPException(401): Если имя пользователя или пароль неверны
        HTTPException(500): В случае внутренней ошибки сервера
    """
    # Находим пользователя
    user = await session.execute(
        select(User).where(User.username == form_data.username)
    )
    user = user.scalar_one_or_none()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверное имя пользователя или пароль",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Получаем активные сессии пользователя
    active_sessions = await session.execute(
        select(UserSession)
        .where(UserSession.user_id == user.id, UserSession.is_active == True)
        .order_by(UserSession.last_activity.asc())
    )
    sessions = active_sessions.scalars().all()
    logger.debug(f"Found {len(sessions)} active sessions for user {user.id}")
    
    # Если активных сессий больше или равно лимиту, деактивируем самую старую
    if len(sessions) >= MAX_SESSIONS_PER_USER:
        oldest_session = sessions[0]
        oldest_session.is_active = False
        await session.commit()
        logger.debug(f"Deactivated old session {oldest_session.id}")
    
    # Создаем токен и сессию
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(user.id)}, expires_delta=access_token_expires
    )
    logger.debug(f"Created access token for user {user.id}: {access_token}")
    
    # Сохраняем информацию о сессии
    new_session = UserSession(
        user_id=user.id,
        access_token=access_token,
        device_info=client_info["device_info"],
        ip_address=client_info["ip_address"]
    )
    session.add(new_session)
    await session.commit()
    logger.debug(f"Created new session with token: {access_token}")
    
    return {"access_token": access_token, "token_type": "bearer"}

@router.post("/register", response_model=UserResponse)
async def register_user(
    user_data: UserCreate,
    current_admin: User = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_async_session)
):
    """
    Регистрация нового пользователя (только для администраторов)
    
    Параметры:
    - username: имя пользователя (обязательное)
    - password: пароль пользователя (обязательное)
    - is_admin: флаг администратора (опционально, по умолчанию False)
    
    Returns:
        UserResponse: Информация о созданном пользователе
            - id: уникальный идентификатор пользователя
            - username: имя пользователя
            - is_active: статус активности
            - is_admin: статус администратора
            - created_at: дата и время создания
    
    Raises:
        HTTPException(400): Если пользователь с таким именем уже существует
        HTTPException(401): Если токен авторизации недействителен
        HTTPException(403): Если у пользователя нет прав администратора
    """
    # Проверяем, существует ли пользователь
    existing_user = await session.execute(
        select(User).where(User.username == user_data.username)
    )
    if existing_user.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )
    
    # Создаем нового пользователя
    new_user = User(
        username=user_data.username,
        hashed_password=get_password_hash(user_data.password),
        is_admin=user_data.is_admin
    )
    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)
    
    return new_user

@router.get("/sessions", response_model=ActiveSessionsResponse)
async def get_active_sessions(
    current_admin: User = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_async_session)
):
    """
    Получение списка всех активных сессий (только для администраторов)
    
    Returns:
        ActiveSessionsResponse: Список активных сессий
            - total_sessions: общее количество активных сессий
            - sessions: список сессий с информацией:
                - id: идентификатор сессии
                - device_info: информация об устройстве
                - ip_address: IP-адрес
                - created_at: время создания сессии
                - last_activity: время последней активности
                - duration: продолжительность сессии
                - username: имя пользователя
    
    Raises:
        HTTPException(401): Если токен авторизации недействителен
        HTTPException(403): Если у пользователя нет прав администратора
        HTTPException(500): В случае внутренней ошибки сервера
    """
    try:
        # Получаем все активные сессии с информацией о пользователях
        result = await session.execute(
            select(UserSession, User)
            .join(User, UserSession.user_id == User.id)
            .where(UserSession.is_active == True)
            .order_by(UserSession.last_activity.desc())
        )
        session_users = result.all()
        
        # Форматируем информацию о сессиях и проверяем валидность токенов
        session_info = []
        sessions_to_deactivate = []
        
        for sess, user in session_users:
            try:
                # Пробуем декодировать токен
                decode_token(sess.access_token)
                
                # Если токен валидный, добавляем сессию в список
                duration = sess.last_activity - sess.created_at
                session_info.append(
                    SessionInfo(
                        id=sess.id,
                        device_info=sess.device_info,
                        ip_address=sess.ip_address,
                        created_at=sess.created_at,
                        last_activity=sess.last_activity,
                        duration=str(duration),
                        username=user.username
                    )
                )
            except Exception as e:
                # Если токен невалидный, добавляем сессию в список на деактивацию
                logger.info(f"Сессия {sess.id} имеет невалидный токен: {str(e)}")
                sessions_to_deactivate.append(sess)
        
        # Деактивируем сессии с истекшими токенами
        if sessions_to_deactivate:
            for expired_session in sessions_to_deactivate:
                expired_session.is_active = False
                logger.info(f"Деактивирована сессия {expired_session.id} с истекшим токеном")
            
            await session.commit()
        
        return ActiveSessionsResponse(
            total_sessions=len(session_info),
            sessions=session_info
        )
        
    except Exception as e:
        logger.error(f"Ошибка при получении списка сессий: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при получении списка сессий"
        )

@router.delete("/sessions/{session_id}")
async def terminate_session(
    session_id: int,
    current_admin: User = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_async_session)
):
    """
    Принудительное завершение сессии пользователя (только для администраторов)
    
    Параметры:
    - session_id: идентификатор сессии для завершения
    
    Returns:
        dict: Сообщение об успешном завершении сессии
    
    Raises:
        HTTPException(401): Если токен авторизации недействителен
        HTTPException(403): Если у пользователя нет прав администратора
        HTTPException(404): Если сессия не найдена
    """
    # Находим сессию
    user_session = await session.execute(
        select(UserSession).where(
            UserSession.id == session_id,
            UserSession.is_active == True
        )
    )
    user_session = user_session.scalar_one_or_none()
    
    if not user_session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
    
    # Деактивируем сессию
    user_session.is_active = False
    await session.commit()
    
    return {"message": "Session terminated successfully"}

@router.get("/test", response_model=dict)
async def test_user_endpoint(
    current_user: User = Depends(get_current_user)
):
    """
    Тестовый эндпоинт для проверки авторизации
    
    Returns:
        dict: Тестовое сообщение с информацией о пользователе
            - message: приветственное сообщение с именем пользователя
            - timestamp: текущее время
    
    Raises:
        HTTPException(401): Если токен авторизации недействителен
    """
    return {
        "message": f"Привет, {current_user.username}! Это тестовое уведомление для обычных пользователей.",
        "timestamp": datetime.utcnow()
    }

@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    current_admin: User = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_async_session)
):
    """
    Удаление пользователя из системы (только для администраторов)
    
    Параметры:
    - user_id: идентификатор пользователя для удаления
    
    Returns:
        None: Статус 204 No Content при успешном удалении
    
    Raises:
        HTTPException(400): Если попытка удалить администратора
        HTTPException(401): Если токен авторизации недействителен
        HTTPException(403): Если у пользователя нет прав администратора
        HTTPException(404): Если пользователь не найден
        HTTPException(500): В случае внутренней ошибки сервера
    """
    # Проверяем, существует ли пользователь
    user = await session.execute(
        select(User).where(User.id == user_id)
    )
    user = user.scalar_one_or_none()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Пользователь не найден"
        )
    
    if user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Невозможно удалить администратора"
        )
    
    try:
        # Получаем все активные сессии пользователя
        user_sessions = await session.execute(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.is_active == True
            )
        )
        sessions = user_sessions.scalars().all()
        
        # Деактивируем каждую сессию
        for user_session in sessions:
            user_session.is_active = False
        
        # Удаляем пользователя
        await session.delete(user)
        await session.commit()
        
        logger.info(f"Пользователь {user.username} (ID: {user_id}) был удален администратором {current_admin.username}")
    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка при удалении пользователя {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при удалении пользователя"
        )

@router.get("/users", response_model=UserListResponse)
async def get_all_users(
    current_admin: User = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_async_session)
):
    """
    Получение списка всех пользователей системы (только для администраторов)
    
    Returns:
        UserListResponse: Список всех пользователей
            - all_users: общее количество пользователей
            - users: список пользователей с информацией:
                - id: идентификатор пользователя
                - username: имя пользователя
                - is_active: статус активности
                - is_admin: статус администратора
                - created_at: дата создания
    
    Raises:
        HTTPException(401): Если токен авторизации недействителен
        HTTPException(403): Если у пользователя нет прав администратора
        HTTPException(500): В случае внутренней ошибки сервера
    """
    try:
        # Получаем всех пользователей
        result = await session.execute(
            select(User).order_by(User.username)
        )
        users = result.scalars().all()
        
        return UserListResponse(
            all_users=len(users),
            users=users
        )
    except Exception as e:
        logger.error(f"Ошибка при получении списка пользователей: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при получении списка пользователей"
        )

@router.post("/token/refresh", response_model=Token)
async def refresh_token(
    token_data: TokenRefresh,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    client_info: dict = Depends(get_client_info)
):
    """
    Обновление токена доступа
    
    Параметры:
    - old_token: текущий токен доступа
    
    Returns:
        Token: Новый токен доступа
            - access_token: новый JWT токен
            - token_type: тип токена (всегда "bearer")
    
    Raises:
        HTTPException(401): Если пользователь не найден или неактивен
        HTTPException(404): Если сессия не найдена или неактивна
        HTTPException(500): В случае внутренней ошибки сервера
    """
    try:
        # Очищаем токен от префикса Bearer если он есть
        old_token = token_data.old_token
        if old_token.startswith("Bearer "):
            old_token = old_token[7:]

        # Находим сессию с этим токеном
        user_session = await session.execute(
            select(UserSession).where(
                UserSession.access_token == old_token,
                UserSession.is_active == True
            )
        )
        user_session = user_session.scalar_one_or_none()

        if not user_session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Сессия не найдена или неактивна"
            )

        # Получаем пользователя
        user = await session.execute(
            select(User).where(User.id == user_session.user_id)
        )
        user = user.scalar_one_or_none()

        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Пользователь не найден или неактивен"
            )

        # Создаем новый токен
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        new_token = create_access_token(
            data={"sub": str(user.id)}, expires_delta=access_token_expires
        )

        # Обновляем информацию о сессии
        user_session.access_token = new_token
        user_session.last_activity = datetime.utcnow()
        user_session.device_info = client_info["device_info"]
        user_session.ip_address = client_info["ip_address"]
        
        await session.commit()
        logger.info(f"Токен обновлен для пользователя {user.username}")

        return {"access_token": new_token, "token_type": "bearer"}

    except Exception as e:
        logger.error(f"Ошибка при обновлении токена: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при обновлении токена"
        )

@router.post("/logout")
async def logout(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
    request: Request = None
):
    """
    Завершение текущей сессии пользователя (logout)
    
    Returns:
        dict: Сообщение об успешном завершении сессии
    
    Raises:
        HTTPException(401): Если токен авторизации недействителен
        HTTPException(404): Если сессия не найдена
        HTTPException(500): В случае внутренней ошибки сервера
    """
    try:
        # Получаем токен из заголовка
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Отсутствует токен авторизации"
            )
        
        token = auth_header.split(" ")[1]
        
        # Находим и деактивируем текущую сессию
        user_session = await session.execute(
            select(UserSession).where(
                UserSession.user_id == current_user.id,
                UserSession.access_token == token,
                UserSession.is_active == True
            )
        )
        user_session = user_session.scalar_one_or_none()
        
        if not user_session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Активная сессия не найдена"
            )
        
        # Деактивируем сессию
        user_session.is_active = False
        await session.commit()
        
        logger.info(f"Пользователь {current_user.username} успешно завершил сессию")
        
        return {"message": "Сессия успешно завершена"}
        
    except Exception as e:
        logger.error(f"Ошибка при завершении сессии: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при завершении сессии"
        ) 