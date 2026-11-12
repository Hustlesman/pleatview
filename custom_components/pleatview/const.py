"""Constants for the PleatView integration."""

from typing import Final

DOMAIN: Final = "pleatview"
DEFAULT_PORT: Final = 8765

# Older Home Assistant has published XSS bugs in dashboard cards, which on a TV would run
# all day with the TV's token; the last of them was fixed in 2026.7.
SAFE_HA_VERSION: Final = (2026, 7)
ISSUE_OLD_HA: Final = "old_home_assistant"

# Config entry data
CONF_TOKEN: Final = "token"  # control-API token issued by the TV when pairing
CONF_AUTH_MODE: Final = "auth_mode"  # how the TV signs in to Home Assistant
CONF_HA_TOKEN: Final = "ha_token"  # long-lived access token handed to the TV
CONF_USER_ID: Final = "user_id"  # user created for the TV in managed mode
CONF_ALLOW_CONTROL: Final = "allow_control"

AUTH_MANAGED: Final = "managed"
AUTH_TOKEN: Final = "token"
AUTH_NONE: Final = "none"

# Options, mirrored to the TV app's configuration
CONF_HA_URL: Final = "ha_url"
CONF_HOME_DASHBOARD: Final = "home_dashboard"
CONF_DASHBOARDS: Final = "dashboards"
CONF_KIOSK: Final = "kiosk"
CONF_ZOOM: Final = "zoom"
CONF_ROTATE_SECONDS: Final = "rotate_seconds"
CONF_RETURN_HOME_MINUTES: Final = "return_home_minutes"
CONF_REOPEN_MINUTES: Final = "reopen_minutes"
CONF_RELOAD_MINUTES: Final = "reload_minutes"
CONF_KEEP_SCREEN_ON: Final = "keep_screen_on"
CONF_LAUNCH_ON_BOOT: Final = "launch_on_boot"
# Address of the PleatView Renderer that runs this TV's dashboards; empty: the TV runs them.
CONF_RENDER_URL: Final = "render_url"
# A PleatView Renderer's own entry (renderers.py): what it is, and how it's reached.
CONF_KIND: Final = "kind"
KIND_RENDERER: Final = "renderer"
CONF_SCHEME: Final = "scheme"
CONF_FINGERPRINT: Final = "fingerprint"
RENDER_AUTO: Final = "auto"
RENDERER_PREFIX: Final = "renderer:"
RENDERER_SERVICE: Final = "_pleatview-renderer._tcp.local."

KIOSK_MODES: Final = ["full", "sidebar", "off"]

# Notifications: a notice is a card; a warning adds a short tone over whatever plays; an
# alert adds a stronger one and pauses what plays. The TV makes the tones itself.
CONF_WARNING_TONE: Final = "warning_tone"
CONF_ALERT_TONE: Final = "alert_tone"
LEVELS: Final = ["notice", "warning", "alert"]
WARNING_TONES: Final = ["chime", "ding", "beep", "none"]
ALERT_TONES: Final = ["alarm", "siren", "bell", "none"]
TONES: Final = [*WARNING_TONES[:-1], *ALERT_TONES]

DEFAULT_OPTIONS: Final = {
    CONF_KIOSK: "full",
    CONF_ZOOM: 100,
    CONF_ROTATE_SECONDS: 0,
    CONF_RETURN_HOME_MINUTES: 0,
    CONF_REOPEN_MINUTES: 10,
    CONF_RELOAD_MINUTES: 0,
    CONF_KEEP_SCREEN_ON: True,
    CONF_LAUNCH_ON_BOOT: True,
    CONF_WARNING_TONE: "chime",
    CONF_ALERT_TONE: "alarm",
}

# Media types understood by media_player.play_media in addition to the standard ones
MEDIA_TYPE_DASHBOARD: Final = "dashboard"
MEDIA_TYPE_WEB: Final = "web"
DASHBOARDS_ROOT: Final = "pleatview://dashboards"

MODES: Final = ["fullscreen", "pip", "background"]
POSITIONS: Final = ["top-left", "top-right", "bottom-left", "bottom-right", "center"]

HLS_MIME: Final = "application/vnd.apple.mpegurl"
