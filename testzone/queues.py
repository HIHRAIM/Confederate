import asyncio

class RelayQueues:
    def __init__(self):
        # Legacy queues (for backward compatibility)
        self.discord_to_telegram = asyncio.Queue()
        self.telegram_to_discord = asyncio.Queue()
        self.telegram_to_telegram = asyncio.Queue()
        self.bridge_discord_to_telegram = asyncio.Queue()
        self.bridge_telegram_to_discord = asyncio.Queue()
        self.bridge_discord_edit_delete = asyncio.Queue()
        self.bridge_telegram_edit_delete = asyncio.Queue()
        
        # New queues for the bridge system
        self.bridge_relay_discord = asyncio.Queue()
        self.bridge_relay_telegram = asyncio.Queue()
        self.bridge_edit_discord = asyncio.Queue() 
        self.bridge_edit_telegram = asyncio.Queue()
        self.bridge_delete_discord = asyncio.Queue()
        self.bridge_delete_telegram = asyncio.Queue()
