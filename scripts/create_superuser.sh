#!/bin/bash

# Загружаем переменные из файла .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo ".env file not found!"
    exit 1
fi

# Проверяем наличие переменных суперпользователя
if [ -z "$SUPERUSER_NAME" ] || [ -z "$SUPERUSER_PASSWORD" ]; then
    echo "Error: SUPERUSER_NAME and SUPERUSER_PASSWORD must be set in .env file"
    exit 1
fi

# Генерируем хешированный пароль с помощью Python и bcrypt
HASHED_PASSWORD=$(python3 -c '
import bcrypt
import sys

def hash_password(password):
    # Используем rounds=12 (2**12) как в passlib по умолчанию
    salt = bcrypt.gensalt(rounds=12)
    # Добавляем префикс $2b$ для совместимости с passlib
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

print(hash_password("'"$SUPERUSER_PASSWORD"'"))
')

# SQL для создания суперпользователя
SQL_COMMAND="
INSERT INTO users (username, hashed_password, is_active, is_admin, created_at)
VALUES ('$SUPERUSER_NAME', '$HASHED_PASSWORD', true, true, CURRENT_TIMESTAMP)
ON CONFLICT (username) 
DO UPDATE SET 
    hashed_password = EXCLUDED.hashed_password,
    is_active = true,
    is_admin = true;
"

# Экспортируем пароль для использования в psql
export PGPASSWORD="$DB_PASS"

# Выполнение команды SQL
psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "$SQL_COMMAND"

if [ $? -eq 0 ]; then
    echo "Superuser '$SUPERUSER_NAME' has been created/updated successfully."
else
    echo "Failed to create/update superuser."
fi

# Удаляем переменную окружения
unset PGPASSWORD 