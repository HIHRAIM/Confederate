from typing import List, Dict, Tuple, Optional, Set, Any
from bridge import Bridge

class BridgeManager:
    """
    Manages multiple bridges between Discord and Telegram.
    Each bridge can connect multiple Discord channels with multiple Telegram topics.
    """
    
    def __init__(self, bridges_config):
        self.bridges = []
        
        # Create Bridge objects from config
        for config in bridges_config:
            bridge = Bridge(
                name=config.get("name", "Unnamed Bridge"),
                discord_channels=config["discord_channels"],
                telegram_targets=config["telegram_targets"]
            )
            self.bridges.append(bridge)
    
    def find_bridges_for_discord_channel(self, channel_id: int) -> List[Bridge]:
        """Find all bridges that include this Discord channel."""
        return [bridge for bridge in self.bridges if bridge.contains_discord_channel(channel_id)]
    
    def find_bridges_for_telegram_target(self, chat_id: int, topic_id: Optional[int]) -> List[Bridge]:
        """Find all bridges that include this Telegram target."""
        return [bridge for bridge in self.bridges if bridge.contains_telegram_target(chat_id, topic_id)]
    
    async def relay_from_discord(self, discord_bot, telegram_app, 
                               channel_id: int, message_id: int, body: str) -> List[Tuple]:
        """
        Relay a Discord message to all connected Telegram targets across all relevant bridges.
        Returns a list of all sent Telegram messages.
        """
        bridges = self.find_bridges_for_discord_channel(channel_id)
        all_sent = []
        
        for bridge in bridges:
            sent = await bridge.relay_from_discord(discord_bot, telegram_app, channel_id, message_id, body)
            all_sent.extend(sent)
            
        return all_sent
    
    async def relay_from_telegram(self, discord_bot, telegram_app,
                                chat_id: int, topic_id: Optional[int], 
                                message_id: int, body: str) -> List[Tuple]:
        """
        Relay a Telegram message to all connected Discord channels across all relevant bridges.
        Returns a list of all sent Discord messages.
        """
        bridges = self.find_bridges_for_telegram_target(chat_id, topic_id)
        all_sent = []
        
        for bridge in bridges:
            sent = await bridge.relay_from_telegram(discord_bot, telegram_app, chat_id, topic_id, message_id, body)
            all_sent.extend(sent)
            
        return all_sent
    
    async def edit_discord_message(self, discord_bot, telegram_app,
                                 channel_id: int, message_id: int, body: str) -> List[Tuple]:
        """
        Edit all Telegram copies of a Discord message across all bridges.
        Returns a list of all edited Telegram messages.
        """
        bridges = self.find_bridges_for_discord_channel(channel_id)
        all_edited = []
        
        for bridge in bridges:
            edited = await bridge.edit_discord_message(discord_bot, telegram_app, channel_id, message_id, body)
            all_edited.extend(edited)
            
        return all_edited
    
    async def edit_telegram_message(self, discord_bot, telegram_app,
                                  chat_id: int, topic_id: Optional[int], 
                                  message_id: int, body: str) -> List[Tuple]:
        """
        Edit all Discord copies of a Telegram message across all bridges.
        Returns a list of all edited Discord messages.
        """
        bridges = self.find_bridges_for_telegram_target(chat_id, topic_id)
        all_edited = []
        
        for bridge in bridges:
            edited = await bridge.edit_telegram_message(discord_bot, telegram_app, chat_id, topic_id, message_id, body)
            all_edited.extend(edited)
            
        return all_edited
    
    async def delete_discord_message(self, discord_bot, telegram_app,
                                   channel_id: int, message_id: int) -> List[Tuple]:
        """
        Delete all Telegram copies of a Discord message across all bridges.
        Returns a list of all deleted Telegram messages.
        """
        bridges = self.find_bridges_for_discord_channel(channel_id)
        all_deleted = []
        
        for bridge in bridges:
            deleted = await bridge.delete_discord_message(discord_bot, telegram_app, channel_id, message_id)
            all_deleted.extend(deleted)
            
        return all_deleted
    
    async def delete_telegram_message(self, discord_bot, telegram_app,
                                    chat_id: int, topic_id: Optional[int],
                                    message_id: int) -> List[Tuple]:
        """
        Delete all Discord copies of a Telegram message across all bridges.
        Returns a list of all deleted Discord messages.
        """
        bridges = self.find_bridges_for_telegram_target(chat_id, topic_id)
        all_deleted = []
        
        for bridge in bridges:
            deleted = await bridge.delete_telegram_message(discord_bot, telegram_app, chat_id, topic_id, message_id)
            all_deleted.extend(deleted)
            
        return all_deleted
