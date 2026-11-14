# VIP TASK

VIP TASK — маркетплейс заданий с основным интерфейсом в Telegram Mini App.

## Архитектура

- **Приложение:** FastAPI, точка входа `app:app`.
- **Telegram:** aiogram 3 с long polling; бот открывает Mini App по кнопке `/start`.
- **База данных:** PostgreSQL.
- **Интерфейс:** `index.html`, размещается тем же FastAPI-сервисом.
- **Проверка сроков:** фоновый worker в процессе приложения.

Для long polling используй **один экземпляр** приложения.

## Локальные проверки

Установить зависимости:

```bash
pip install -r requirements.txt
```

Запустить проверки (для интеграционных тестов нужен PostgreSQL и `DATABASE_URL`):

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

## Развёртывание

Подробная инструкция по Railway, переменным окружения, домену Mini App и проверке запуска находится в [RAILWAY_SETUP.md](RAILWAY_SETUP.md).

Не добавляй `BOT_TOKEN` или `DATABASE_URL` в GitHub. Вводи секреты только в Variables Railway.
