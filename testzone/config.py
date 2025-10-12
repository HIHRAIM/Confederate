DISCORD_TOKEN = "ODg4MzE0Njg5ODI0NjM2OTk4.Gqlx2z.ObHpBClfqBi747ZJmFGKCwP8_aAwdZnTnJeTSs"
TELEGRAM_TOKEN = "7682919156:AAHmb735x5K8VEvCr35DJ-sN0JY3uC8DhTc"

# Define bridges where each bridge can connect multiple Discord channels 
# with multiple Telegram topics
BRIDGES = [
    # Main bridge (replaces the previous global bridge)
    {
        "name": "Main Bridge",
        "discord_channels": [
            869581909045444687,
            1380899919077834803,
            640638068164001823
        ],
        "telegram_targets": [
            {"chat_id": -1002336919485, "topic_id": 1936},
            {"chat_id": -1002262445485, "topic_id": 1335},
            {"chat_id": -1002775568603, "topic_id": 28},
        ]
    },
    
    # Клуб Вещания bridge
    {
        "name": "Клуб Вещания",
        "discord_channels": [640895347652296704],  # телерадио
        "telegram_targets": [{"chat_id": -1002262445485, "topic_id": 8}]
    },
    
    # Медиахранилище bridge
    {
        "name": "Медиахранилище",
        "discord_channels": [786603722784505866],  # медиахранилище
        "telegram_targets": [{"chat_id": -1002262445485, "topic_id": 36}]
    },
    
    # Фанработы bridge
    {
        "name": "Фанработы",
        "discord_channels": [709989257153872014],  # фанработы
        "telegram_targets": [{"chat_id": -1002262445485, "topic_id": 37}]
    },

    # Все языки
    {
        "name": "Все языки",
        "discord_channels": [
            1020371913450717255,
            1404217771398533130
            ], 
        "telegram_targets": [{"chat_id": -1002336919485, "topic_id": 13384}]
    },
    
    # Convert all other existing EXTRA_BRIDGES to this format...
]

# For backward compatibility during migration, keep these but don't use them in new code
DISCORD_CHANNEL_IDS = [
    869581909045444687,
    1380899919077834803,
    640638068164001823
]

TELEGRAM_TARGETS = [
    {"chat_id": -1002336919485, "topic_id": 1936},
    {"chat_id": -1002262445485, "topic_id": 1335},
    {"chat_id": -1002775568603, "topic_id": 28},
]

EXTRA_BRIDGES = [
    {
        "discord_channel_id": 640895347652296704, #телерадио (Клуб Вещания)
        "telegram_chat_id": -1002262445485,
        "telegram_topic_id": 8,
    },
    # ... rest of EXTRA_BRIDGES as they are
]