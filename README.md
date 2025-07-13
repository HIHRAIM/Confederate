# Confederate

**Confederate** — это Python-проект для организации взаимодействия между Discord и Telegram, а также для проведения голосований и обратной связи в рамках мероприятий (например, Supraconfedetative Wiki Olympiad 2025).

## Основные возможности

- **Релей-бот**: Автоматический обмен сообщениями и вложениями между каналами в Discord и темами в Telegram.
- **Модуль голосований**: Сбор голосов участников по статьям и дизайнам через команды в Discord.

## Примеры использования

### Релей между Discord и Telegram
- Сообщения, отправленные в указанные каналы Discord, автоматически попадают в соответствующие топики Telegram (и наоборот).
- Поддержка вложений.

### Голосование и обратная связь
- Команды Discord:
  - `/start lang:<ru|uk|pl|en>` — выбор языка.
  - `/vote_articles text:<ваш текст>` — голос за статью.
  - `/vote_designs text:<ваш текст>` — голос за дизайн.
  - `/support text:<ваше обращение>` — вопрос или предложение организаторам.
  - `/ban user_id:<id>` — бан пользователя (только для организаторов).
  - `/unban user_id:<id>` — разбан пользователя.
  - `/accepted key:<ключ>` — одобрить голос.
  - `/denied key:<ключ>, reason:<причина>` — отклонить голос.
  - `/reply key:<ключ>, text:<текст>` — ответ пользователю.

## Установка

1. **Клонируйте репозиторий:**
   ```bash
   git clone https://github.com/HIHRAIM/Confederate.git
   cd Confederate
   ```
2. **Создайте виртуальное окружение и установите зависимости:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Настройте токены и каналы в `relaybot/config.py` и `submain/transmitbot.py`:**
   - Укажите свои Discord и Telegram токены.
   - Настройте нужные ID каналов и топиков.

4. **Запустите бота:**
   ```bash
   python submain/relaybot.py
   python submain/transmitbot.py
   ```

## Зависимости

- Python >= 3.8
- discord.py
- python-telegram-bot
- asyncio

## Лицензия

Лицензия не указана (по состоянию на июль 2025). Для использования и распространения уточняйте у автора.

## Контакты

- Авторы: [HIHRAIM](https://github.com/HIHRAIM) и [Fersteax](https://github.com/Fersteax)
- Репозиторий: [github.com/HIHRAIM/Confederate](https://github.com/HIHRAIM/Confederate)

---

*Документация обновлена ИИ на основе исходного кода и автоконфигурации. Для подробных сценариев интеграции и расширения смотрите исходные файлы `submain/relaybot.py`, `submain/transmitbot.py`, `relaybot/config.py`.*
