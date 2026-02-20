import asyncio

class RelayQueues:
    def __init__(self):
        self.discord_to_telegram = asyncio.Queue()
        self.telegram_to_discord = asyncio.Queue()
        self.telegram_to_telegram = asyncio.Queue()
