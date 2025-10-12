import asyncio
from typing import List, Dict, Tuple, Optional, Set, Any
from utils import format_message

class Bridge:
    """
    Manages a many-to-many bridge between Discord channels and Telegram topics.
    Messages sent in any connected channel/topic are relayed to all other connected channels/topics.
    """
    
    def __init__(self, name: str, discord_channels: List[int], telegram_targets: List[Dict[str, Any]]):
        self.name = name
        self.discord_channels = set(discord_channels)
        self.telegram_targets = []
        
        # Normalize telegram targets to ensure consistent format
        for target in telegram_targets:
            self.telegram_targets.append({
                "chat_id": target["chat_id"],
                "topic_id": target.get("topic_id")
            })
        
        # Message mappings for tracking edits and deletes
        self.discord_to_telegram = {}  # (discord_channel_id, discord_msg_id) -> List[(chat_id, topic_id, tg_msg_id)]
        self.telegram_to_discord = {}  # (chat_id, topic_id, tg_msg_id) -> List[(discord_channel_id, discord_msg_id)]
    
    def contains_discord_channel(self, channel_id: int) -> bool:
        """Check if this bridge contains the specified Discord channel."""
        return channel_id in self.discord_channels
    
    def contains_telegram_target(self, chat_id: int, topic_id: Optional[int]) -> bool:
        """Check if this bridge contains the specified Telegram target."""
        return any(
            target["chat_id"] == chat_id and target.get("topic_id") == topic_id
            for target in self.telegram_targets
        )
    
    async def relay_from_discord(self, discord_bot, telegram_app, 
                                channel_id: int, message_id: int, body: str) -> List[Tuple]:
        """
        Relay a message from Discord to all connected Telegram targets.
        Returns a list of all sent Telegram messages as (chat_id, topic_id, msg_id) tuples.
        """
        if not self.contains_discord_channel(channel_id):
            return []
        
        sent_messages = []
        
        # Send to all Telegram targets in this bridge
        for target in self.telegram_targets:
            try:
                chat_id = target["chat_id"]
                topic_id = target.get("topic_id")
                
                if topic_id is not None:
                    sent = await telegram_app.bot.send_message(
                        chat_id=chat_id,
                        text=body,
                        message_thread_id=topic_id,
                        parse_mode="HTML"
                    )
                else:
                    sent = await telegram_app.bot.send_message(
                        chat_id=chat_id,
                        text=body,
                        parse_mode="HTML"
                    )
                
                sent_messages.append((chat_id, topic_id, sent.message_id))
                
                # Store mapping for this message
                key = (channel_id, message_id)
                if key not in self.discord_to_telegram:
                    self.discord_to_telegram[key] = []
                self.discord_to_telegram[key].append((chat_id, topic_id, sent.message_id))
                
                # Store reverse mapping
                key_reverse = (chat_id, topic_id, sent.message_id)
                if key_reverse not in self.telegram_to_discord:
                    self.telegram_to_discord[key_reverse] = []
                self.telegram_to_discord[key_reverse].append((channel_id, message_id))
                
            except Exception as e:
                print(f"[Bridge '{self.name}'] Discord->Telegram relay error: {e}")
        
        return sent_messages
    
    async def relay_from_telegram(self, discord_bot, telegram_app,
                                 chat_id: int, topic_id: Optional[int], 
                                 message_id: int, body: str) -> List[Tuple]:
        """
        Relay a message from Telegram to all connected Discord channels.
        Returns a list of all sent Discord messages as (channel_id, msg_id) tuples.
        """
        if not self.contains_telegram_target(chat_id, topic_id):
            return []
        
        sent_messages = []
        
        # Send to all Discord channels in this bridge
        for channel_id in self.discord_channels:
            try:
                channel = discord_bot.get_channel(channel_id)
                if channel:
                    sent = await channel.send(body)
                    sent_messages.append((channel_id, sent.id))
                    
                    # Store mapping for this message
                    key = (chat_id, topic_id, message_id)
                    if key not in self.telegram_to_discord:
                        self.telegram_to_discord[key] = []
                    self.telegram_to_discord[key].append((channel_id, sent.id))
                    
                    # Store reverse mapping
                    key_reverse = (channel_id, sent.id)
                    if key_reverse not in self.discord_to_telegram:
                        self.discord_to_telegram[key_reverse] = []
                    self.discord_to_telegram[key_reverse].append((chat_id, topic_id, message_id))
                else:
                    print(f"[Bridge '{self.name}'] Channel {channel_id} not found")
            except Exception as e:
                print(f"[Bridge '{self.name}'] Telegram->Discord relay error: {e}")
        
        return sent_messages
    
    async def edit_discord_message(self, discord_bot, telegram_app,
                                 channel_id: int, message_id: int, body: str) -> List[Tuple]:
        """
        Edit all Telegram copies of a Discord message in this bridge.
        Returns a list of edited messages as (chat_id, topic_id, msg_id) tuples.
        """
        key = (channel_id, message_id)
        if key not in self.discord_to_telegram:
            return []
        
        edited_messages = []
        
        for chat_id, topic_id, tg_msg_id in self.discord_to_telegram[key]:
            try:
                await telegram_app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=tg_msg_id,
                    text=body,
                    parse_mode="HTML"
                )
                edited_messages.append((chat_id, topic_id, tg_msg_id))
            except Exception as e:
                print(f"[Bridge '{self.name}'] Discord->Telegram edit error: {e}")
        
        return edited_messages
    
    async def edit_telegram_message(self, discord_bot, telegram_app,
                                  chat_id: int, topic_id: Optional[int], 
                                  message_id: int, body: str) -> List[Tuple]:
        """
        Edit all Discord copies of a Telegram message in this bridge.
        Returns a list of edited messages as (channel_id, msg_id) tuples.
        """
        key = (chat_id, topic_id, message_id)
        if key not in self.telegram_to_discord:
            return []
        
        edited_messages = []
        
        for channel_id, discord_msg_id in self.telegram_to_discord[key]:
            try:
                channel = discord_bot.get_channel(channel_id)
                if channel:
                    discord_msg = await channel.fetch_message(discord_msg_id)
                    await discord_msg.edit(content=body)
                    edited_messages.append((channel_id, discord_msg_id))
            except Exception as e:
                print(f"[Bridge '{self.name}'] Telegram->Discord edit error: {e}")
        
        return edited_messages
    
    async def delete_discord_message(self, discord_bot, telegram_app,
                                   channel_id: int, message_id: int) -> List[Tuple]:
        """
        Delete all Telegram copies of a Discord message in this bridge.
        Returns a list of deleted messages as (chat_id, topic_id, msg_id) tuples.
        """
        key = (channel_id, message_id)
        if key not in self.discord_to_telegram:
            return []
        
        deleted_messages = []
        telegram_messages = self.discord_to_telegram.pop(key, [])
        
        for chat_id, topic_id, tg_msg_id in telegram_messages:
            try:
                await telegram_app.bot.delete_message(
                    chat_id=chat_id,
                    message_id=tg_msg_id
                )
                
                # Clean up reverse mapping
                reverse_key = (chat_id, topic_id, tg_msg_id)
                if reverse_key in self.telegram_to_discord:
                    self.telegram_to_discord[reverse_key] = [
                        (c, m) for c, m in self.telegram_to_discord[reverse_key] 
                        if c != channel_id or m != message_id
                    ]
                    if not self.telegram_to_discord[reverse_key]:
                        self.telegram_to_discord.pop(reverse_key)
                
                deleted_messages.append((chat_id, topic_id, tg_msg_id))
            except Exception as e:
                print(f"[Bridge '{self.name}'] Discord->Telegram delete error: {e}")
        
        return deleted_messages
    
    async def delete_telegram_message(self, discord_bot, telegram_app,
                                    chat_id: int, topic_id: Optional[int],
                                    message_id: int) -> List[Tuple]:
        """
        Delete all Discord copies of a Telegram message in this bridge.
        Returns a list of deleted messages as (channel_id, msg_id) tuples.
        """
        key = (chat_id, topic_id, message_id)
        if key not in self.telegram_to_discord:
            return []
        
        deleted_messages = []
        discord_messages = self.telegram_to_discord.pop(key, [])
        
        for channel_id, discord_msg_id in discord_messages:
            try:
                channel = discord_bot.get_channel(channel_id)
                if channel:
                    discord_msg = await channel.fetch_message(discord_msg_id)
                    await discord_msg.delete()
                    
                    # Clean up reverse mapping
                    reverse_key = (channel_id, discord_msg_id)
                    if reverse_key in self.discord_to_telegram:
                        self.discord_to_telegram[reverse_key] = [
                            (c, t, m) for c, t, m in self.discord_to_telegram[reverse_key]
                            if c != chat_id or t != topic_id or m != message_id
                        ]
                        if not self.discord_to_telegram[reverse_key]:
                            self.discord_to_telegram.pop(reverse_key)
                    
                    deleted_messages.append((channel_id, discord_msg_id))
            except Exception as e:
                print(f"[Bridge '{self.name}'] Telegram->Discord delete error: {e}")
        
        return deleted_messages
