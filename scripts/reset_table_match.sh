#!/bin/bash

# Загружаем переменные из файла .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo ".env file not found!"
    exit 1
fi

# SQL для очистки данных
SQL_COMMAND="
TRUNCATE TABLE match RESTART IDENTITY CASCADE;
"

# Экспортируем пароль для использования в psql
export PGPASSWORD="$DB_PASS"

# Выполнение команды SQL
psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "$SQL_COMMAND"

if [ $? -eq 0 ]; then
    echo "Table 'match' has been clear successfully."
else
    echo "Failed to clear table 'match'."
fi

# Удаляем переменную окружения
unset PGPASSWORD