#!/bin/bash

# Параметры подключения к базе данных
DB_NAME="china_parser"
DB_USER="root"
DB_PASSWORD="q1726354"
DB_HOST="localhost"
DB_PORT="5432"

# Запись SQL для добавления данных
SQL_COMMAND="
INSERT INTO league (id, name) VALUES
(1, 'ipbl pro division'),
(2, 'ipbl pro division women'),
(3, 'rocket basketball league'),
(4, 'rocket basketball league women');
"

# Экспортируем пароль для использования в psql
export PGPASSWORD="$DB_PASSWORD"

# Выполнение команды SQL
psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "$SQL_COMMAND"

# Удаляем переменную окружения
unset PGPASSWORD