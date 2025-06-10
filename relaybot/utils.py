def format_message(platform, group_name, username, text, reply_to=None, repost=None, attachments=None):
    """
    Format a message for forwarding.

    - platform: "Discord" or "Telegram"
    - group_name: name of the server or group (not channel/topic)
    - username: plain display name (no @)
    - text: message text
    - reply_to: display name of the user being replied to, or extracted text if replying to a bot message
    - repost: string describing original author/source if this is a repost, else None
    - attachments: list of attachment URLs (optional)
    """
    lines = [f"[{platform} | {group_name}] {username}:"]
    if repost:
        lines.append(repost)
    if reply_to:
        lines.append(f"(отвечая {reply_to})")
    lines.append(text)
    if attachments:
        lines.append("\n".join(attachments))
    return "\n".join(lines)