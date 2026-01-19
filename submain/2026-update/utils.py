from config import ADMINS

def is_admin(platform, user_id):
    return user_id in ADMINS.get(platform, set())


def extract_username_from_bot_message(text: str):
    try:
        return text.split("]", 1)[1].split(":", 1)[0].strip()
    except Exception:
        return None
