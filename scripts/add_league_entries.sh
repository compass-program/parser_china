#!/bin/bash

# Загружаем переменные из файла .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo ".env file not found!"
    exit 1
fi

# SQL для добавления данных
SQL_COMMAND="
INSERT INTO league (id, name) VALUES
(1, 'ipbl pro division'),
(2, 'ipbl pro division women'),
(3, 'rocket basketball league'),
(4, 'rocket basketball league women');
"

# Экспортируем пароль для использования в psql
export PGPASSWORD="$DB_PASS"

# Выполнение команды SQL
psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "$SQL_COMMAND"

if [ $? -eq 0 ]; then
    echo "Table 'league' has been init successfully."
else
    echo "Failed to init table 'league'."
fi

# Удаляем переменную окружения
unset PGPASSWORD