"""Constants for Telegram Access Registrar."""

DOMAIN = "telegram_access_registrar"
PLATFORMS: list[str] = []

CONF_BOT_TOKEN = "bot_token"
CONF_ADMIN_CHAT_ID = "admin_chat_id"

SERVICE_APPROVE = "approve"
SERVICE_REJECT = "reject"
SERVICE_SEND_MESSAGE = "send_message"
SERVICE_LIST_REQUESTS = "list_requests"
SERVICE_ISSUE_AKUVOX_KEY = "issue_akuvox_key"

EVENT_ACCESS_REQUESTED = f"{DOMAIN}_access_requested"
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.registry"

CALLBACK_PREFIX = "tar"
COMMAND_START = "/start"
COMMAND_ACCESS = "/access"
