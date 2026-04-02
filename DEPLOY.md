# Деплой на сервер 24/7

Это минимальная инструкция для переноса бота на Linux VPS с `systemd`.

## Что понадобится

- сервер Ubuntu 22.04+;
- доступ по SSH;
- Python 3.10+;
- токены и секретный код из локального `.env`.

## 1. Подключитесь к серверу

```bash
ssh user@your-server-ip
```

## 2. Установите пакеты

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
```

## 3. Клонируйте репозиторий

```bash
git clone https://github.com/Dariatravel/vk-comment-monitor-bot.git
cd vk-comment-monitor-bot
```

Если репозиторий приватный, используйте HTTPS с авторизацией или SSH-ключ.

## 4. Создайте окружение и установите зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 5. Создайте `.env`

```bash
cp .env.example .env
```

Заполните:

- `VK_GROUP_ID`
- `VK_GROUP_TOKEN`
- `VK_READER_TOKEN`
- `VK_READER_TOKEN_TTL_SECONDS`
- `VK_READER_TOKEN_WARN_BEFORE_SECONDS`
- `ALLOWED_USER_ID`
- `STRICT_DIALOG_MODE`
- `ACCESS_CODE`

## 6. Проверьте ручной запуск

```bash
source .venv/bin/activate
python3 bot.py
```

Если бот стартует без ошибки, остановите его `Ctrl+C`.

## 7. Установите `systemd`-сервис

Скопируйте пример:

```bash
sudo cp deploy/vk-comment-monitor.service /etc/systemd/system/vk-comment-monitor.service
```

Откройте сервис и при необходимости замените путь `/opt/vk-comment-monitor` на ваш реальный путь:

```bash
sudo nano /etc/systemd/system/vk-comment-monitor.service
```

Рекомендуемый рабочий путь на сервере:

```bash
/opt/vk-comment-monitor
```

Если хотите, сначала перенесите проект именно туда:

```bash
sudo mkdir -p /opt/vk-comment-monitor
sudo chown -R "$USER":"$USER" /opt/vk-comment-monitor
git clone https://github.com/Dariatravel/vk-comment-monitor-bot.git /opt/vk-comment-monitor
cd /opt/vk-comment-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 8. Включите автозапуск

```bash
sudo systemctl daemon-reload
sudo systemctl enable vk-comment-monitor
sudo systemctl start vk-comment-monitor
```

## 9. Проверьте статус

```bash
sudo systemctl status vk-comment-monitor
```

## 10. Смотрите лог

```bash
journalctl -u vk-comment-monitor -f
```

## Обновление бота

```bash
cd /opt/vk-comment-monitor
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart vk-comment-monitor
```

## Важно

- `.env` не храните в GitHub;
- если токен уже светился в переписке, выпустите новый перед переносом на сервер;
- для надёжной работы лучше использовать отдельный VPS, а не домашний компьютер.
