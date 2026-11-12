"""Media player for PleatView: cast media and dashboards to the TV."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_ANNOUNCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_EXTRA,
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALERT_TONE,
    CONF_WARNING_TONE,
    DASHBOARDS_ROOT,
    DEFAULT_OPTIONS,
    HLS_MIME,
    LEVELS,
    MEDIA_TYPE_DASHBOARD,
    MEDIA_TYPE_WEB,
    POSITIONS,
    TONES,
)
from .coordinator import PleatViewConfigEntry, PleatViewDevice
from .entity import PleatViewEntity

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SELECT_SOURCE
)

MEDIA_STATES = {
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "buffering": MediaPlayerState.BUFFERING,
}

# Generic media types mapped to the MIME family the TV uses to pick a player.
GENERIC_MIME = {
    MediaType.MUSIC: "audio/*",
    MediaType.TRACK: "audio/*",
    MediaType.PODCAST: "audio/*",
    "audio": "audio/*",
    MediaType.VIDEO: "video/*",
    MediaType.MOVIE: "video/*",
    MediaType.EPISODE: "video/*",
    MediaType.TVSHOW: "video/*",
    MediaType.IMAGE: "image/*",
}

STREAM_MIME_TYPES = (HLS_MIME, "application/x-mpegurl", "application/dash+xml")

DURATION = vol.All(vol.Coerce(int), vol.Range(min=0, max=86400))
SIZE = vol.All(vol.Coerce(int), vol.Range(min=10, max=100))
LAYOUT_SCHEMA = {
    vol.Optional("position"): vol.In(POSITIONS),
    vol.Optional("size"): SIZE,
    vol.Optional("duration"): DURATION,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PleatViewConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the media player and its services."""
    async_add_entities([PleatViewMediaPlayer(entry.runtime_data)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "show_dashboard",
        {vol.Required("path"): cv.string, vol.Optional("duration"): DURATION},
        "async_show_dashboard",
    )
    platform.async_register_entity_service(
        "show_url",
        {
            vol.Required("url"): cv.url,
            vol.Optional("mode"): vol.In(["fullscreen", "pip"]),
            **LAYOUT_SCHEMA,
        },
        "async_show_url",
    )
    platform.async_register_entity_service(
        "show_camera",
        {
            vol.Required("camera"): cv.entity_domain("camera"),
            vol.Optional("mode", default="pip"): vol.In(["fullscreen", "pip"]),
            vol.Optional("title"): cv.string,
            **LAYOUT_SCHEMA,
        },
        "async_show_camera",
    )
    platform.async_register_entity_service(
        "notify",
        {
            vol.Required("message"): cv.string,
            vol.Optional("title"): cv.string,
            vol.Optional("image"): cv.string,
            vol.Optional("camera"): cv.entity_domain("camera"),
            vol.Optional("duration"): vol.All(vol.Coerce(int), vol.Range(min=1, max=3600)),
            vol.Optional("position"): vol.In(POSITIONS),
            vol.Optional("level", default="notice"): vol.In(LEVELS),
            vol.Optional("tone"): vol.In(TONES),
        },
        "async_notify",
    )
    platform.async_register_entity_service("home", {}, "async_home")
    platform.async_register_entity_service("reload", {}, "async_reload_dashboard")
    platform.async_register_entity_service(
        "forget_sign_ins", {vol.Optional("site"): cv.string}, "async_forget_sign_ins"
    )


def _playable(item: BrowseMedia) -> bool:
    content_type = str(item.media_content_type)
    return content_type.startswith(("audio/", "video/", "image/")) or content_type in STREAM_MIME_TYPES


def _layout(source: dict[str, Any]) -> dict[str, Any]:
    return {key: source.get(key) for key in ("mode", "position", "size", "duration")}


class PleatViewMediaPlayer(PleatViewEntity, MediaPlayerEntity):
    """The TV as a media player whose sources are dashboards."""

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_supported_features = SUPPORTED_FEATURES
    # Camera and media links are signed for a day: keep them out of the history.
    _unrecorded_attributes = frozenset({ATTR_MEDIA_CONTENT_ID, "url"})

    def __init__(self, device: PleatViewDevice) -> None:
        """Initialize the media player."""
        super().__init__(device)
        self._attr_unique_id = device.device_id

    # ---- state -----------------------------------------------------------------

    @property
    def _state(self) -> dict[str, Any]:
        return self._device.state

    @property
    def _media(self) -> dict[str, Any] | None:
        return self._state.get("media")

    @property
    def _overlay(self) -> dict[str, Any] | None:
        return self._state.get("overlay")

    @property
    def state(self) -> MediaPlayerState:
        """Off while PleatView is hidden or in standby; idle while showing a dashboard.

        Hidden behind another app, a camera or video it shows in a corner over that app
        counts as playing, so Home Assistant shows it and can stop it.
        """
        if self._state.get("power") == "standby":
            return MediaPlayerState.OFF
        if not self._state.get("foreground"):
            overlay = self._overlay or {}
            return MediaPlayerState.PLAYING if overlay.get("mode") == "pip" else MediaPlayerState.OFF
        if media := self._media:
            return MEDIA_STATES.get(media.get("state"), MediaPlayerState.IDLE)
        if self._overlay:
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        """Return PleatView's playback volume (0..1)."""
        return self._state.get("volume")

    @property
    def is_volume_muted(self) -> bool | None:
        """Return whether playback is muted."""
        return self._state.get("muted")

    @property
    def media_content_id(self) -> str | None:
        """Return the URL being shown or played."""
        if media := self._media:
            return media.get("url")
        if overlay := self._overlay:
            return overlay.get("url")
        return None

    @property
    def media_content_type(self) -> MediaType | str | None:
        """Return the type of what is playing."""
        if media := self._media:
            mime = media.get("mime_type") or ""
            return MediaType.MUSIC if mime.startswith("audio/") or media.get("mode") == "background" else MediaType.VIDEO
        if overlay := self._overlay:
            kind = overlay.get("kind")
            return MediaType.IMAGE if kind == "image" else MediaType.VIDEO if kind == "video" else MediaType.URL
        return None

    @property
    def media_title(self) -> str | None:
        """Return the media title."""
        if media := self._media:
            return media.get("title")
        return None

    @property
    def media_artist(self) -> str | None:
        """Return the artist."""
        if media := self._media:
            return media.get("artist")
        return None

    @property
    def media_image_url(self) -> str | None:
        """Return cover art."""
        if media := self._media:
            return media.get("image_url")
        return None

    @property
    def media_duration(self) -> float | None:
        """Return the duration in seconds (None for live streams)."""
        if media := self._media:
            return media.get("duration")
        return None

    @property
    def media_position(self) -> float | None:
        """Return the position at media_position_updated_at."""
        if media := self._media:
            return media.get("position")
        return None

    @property
    def media_position_updated_at(self):
        """Return when the position was sampled."""
        if (media := self._media) and media.get("updated_at"):
            return dt_util.utc_from_timestamp(media["updated_at"])
        return None

    @property
    def source_list(self) -> list[str]:
        """Return dashboard titles."""
        return [dashboard["title"] for dashboard in self._device.dashboards]

    @property
    def source(self) -> str | None:
        """Return the dashboard on screen (longest matching path)."""
        current = self._state.get("dashboard") or self._state.get("url")
        if not current:
            return None
        best: dict[str, str] | None = None
        for dashboard in self._device.dashboards:
            path = dashboard["path"]
            base = path.rstrip("/")
            if current == path or current.startswith((base + "/", path + "?")):
                if best is None or len(path) > len(best["path"]):
                    best = dashboard
        return best["title"] if best else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose details useful in automations."""
        return {
            "dashboard": self._state.get("dashboard"),
            "url": self._state.get("url"),
            "ha_connected": self._state.get("ha_connected"),
            "auth_error": self._state.get("auth_error"),
            "last_error": self._state.get("last_error"),
            "overlay_permission": self._state.get("overlay_permission"),
            # The dashboard that waits for a username and password on the TV, if any.
            "sign_in_needed": (self._state.get("sign_in") or {}).get("title"),
            "app_version": self._state.get("app_version"),
        }

    # ---- media_player services ------------------------------------------------

    async def async_turn_on(self) -> None:
        """Bring PleatView to the front / leave standby."""
        await self._device.async_command("turn_on")

    async def async_turn_off(self) -> None:
        """Blank the screen and let the TV's own sleep timer take over."""
        await self._device.async_command("turn_off")

    async def async_media_play(self) -> None:
        """Resume playback."""
        await self._device.async_command("play")

    async def async_media_pause(self) -> None:
        """Pause playback."""
        await self._device.async_command("pause")

    async def async_media_stop(self) -> None:
        """Stop media and close overlays."""
        await self._device.async_command("stop")

    async def async_media_seek(self, position: float) -> None:
        """Seek to a position in seconds."""
        await self._device.async_command("seek", position=position)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set PleatView's playback volume."""
        await self._device.async_command("set_volume", level=volume)

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute playback."""
        await self._device.async_command("mute", muted=mute)

    async def async_select_source(self, source: str) -> None:
        """Show a dashboard by title."""
        for dashboard in self._device.dashboards:
            if dashboard["title"] == source:
                await self._device.async_command("show_dashboard", path=dashboard["path"])
                return
        raise ServiceValidationError(f"Unknown dashboard: {source}")

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media, show an image, open a web page or switch dashboards.

        ``extra`` may carry mode (fullscreen/pip/background), position, size,
        duration, title, artist, image_url, loop, volume and start_position.
        """
        extra: dict[str, Any] = kwargs.get(ATTR_MEDIA_EXTRA) or {}
        if media_type == MEDIA_TYPE_DASHBOARD:
            await self._device.async_command(
                "show_dashboard", path=self._device.target(media_id), duration=extra.get("duration")
            )
            return
        if media_type == MEDIA_TYPE_WEB:
            await self._device.async_command("show_url", url=media_id, **_layout(extra))
            return

        mime: str | None = None
        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            media_id, mime = item.url, item.mime_type
        elif "/" in str(media_type):
            mime = str(media_type)
        else:
            mime = GENERIC_MIME.get(media_type)

        await self._device.async_command(
            "play_media",
            url=self._device.media_url(media_id),
            mime_type=mime,
            title=extra.get("title"),
            artist=extra.get("artist"),
            image_url=extra.get("image_url"),
            announce=True if kwargs.get(ATTR_MEDIA_ANNOUNCE) else None,
            loop=extra.get("loop"),
            volume=extra.get("volume"),
            start_position=extra.get("start_position"),
            **_layout(extra),
        )

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse dashboards plus everything in Home Assistant's media sources."""
        if media_content_id == DASHBOARDS_ROOT:
            folder = self._dashboards_folder()
            folder.children = [
                BrowseMedia(
                    media_class=MediaClass.APP,
                    media_content_id=dashboard["path"],
                    media_content_type=MEDIA_TYPE_DASHBOARD,
                    title=dashboard["title"],
                    can_play=True,
                    can_expand=False,
                )
                for dashboard in self._device.dashboards
            ]
            return folder
        if media_content_id:
            return await media_source.async_browse_media(
                self.hass, media_content_id, content_filter=_playable
            )
        dashboards = self._dashboards_folder()
        try:
            # Browse the explicit root: with a single source Home Assistant would
            # otherwise jump straight into it.
            root = await media_source.async_browse_media(
                self.hass, media_source.URI_SCHEME, content_filter=_playable
            )
        except BrowseError as err:
            _LOGGER.debug("Media sources unavailable: %s", err)
            return BrowseMedia(
                media_class=MediaClass.DIRECTORY,
                media_content_id="",
                media_content_type="",
                title=self._device.name,
                can_play=False,
                can_expand=True,
                children=[dashboards],
                children_media_class=MediaClass.DIRECTORY,
            )
        root.children = [dashboards, *(root.children or [])]
        return root

    @staticmethod
    def _dashboards_folder() -> BrowseMedia:
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=DASHBOARDS_ROOT,
            media_content_type="dashboards",
            title="Dashboards",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.APP,
        )

    # ---- PleatView services -----------------------------------------------------

    async def async_show_dashboard(self, path: str, duration: int | None = None) -> None:
        """Show a dashboard, optionally returning home after ``duration`` seconds."""
        await self._device.async_command("show_dashboard", path=self._device.target(path), duration=duration)

    async def async_show_url(self, url: str, **layout: Any) -> None:
        """Open a web page full screen or as picture-in-picture."""
        await self._device.async_command("show_url", url=url, **layout)

    async def async_show_camera(
        self, camera: str, mode: str = "pip", title: str | None = None, **layout: Any
    ) -> None:
        """Show a camera's live stream (HLS), falling back to MJPEG."""
        # Imported here: the camera integration is optional.
        from homeassistant.components.camera import async_request_stream  # noqa: PLC0415

        if title is None and (camera_state := self.hass.states.get(camera)):
            title = camera_state.name
        if self._device.stream_overlays:
            # A screen that only plays the rendered stream gets the camera drawn into it.
            await self._device.async_command(
                "show_image", url=self._device.media_url(f"/api/camera_proxy_stream/{camera}"), mode=mode, **layout)
            return
        try:
            try:
                stream_path = await async_request_stream(self.hass, camera, "hls")
            except ValueError:
                # Home Assistant's stream raises this when it's asked again just as it shuts
                # down an idle HLS output (seen right after a camera's duration ran out).
                await asyncio.sleep(1)
                stream_path = await async_request_stream(self.hass, camera, "hls")
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.debug("No HLS stream for %s (%s); showing MJPEG instead", camera, err)
            await self._device.async_command(
                "show_image",
                url=self._device.media_url(f"/api/camera_proxy_stream/{camera}"),
                mode=mode,
                **layout,
            )
            return
        await self._device.async_command(
            "play_media",
            url=self._device.media_url(stream_path),
            mime_type=HLS_MIME,
            title=title,
            mode=mode,
            # Where the TV can't play the video (its decoder busy with another app's film),
            # the camera's picture stream instead.
            fallback_url=self._device.media_url(f"/api/camera_proxy_stream/{camera}"),
            **layout,
        )

    async def async_notify(
        self,
        message: str,
        title: str | None = None,
        image: str | None = None,
        camera: str | None = None,
        duration: int | None = None,
        position: str | None = None,
        level: str = "notice",
        tone: str | None = None,
    ) -> None:
        """Show a notification card, optionally with an image or camera snapshot.

        A warning also plays a short tone, without touching what plays; an alert plays a
        stronger one and pauses what plays. Their tones are this TV's settings unless the
        call names one.
        """
        if level != "notice" and tone is None:
            options = {**DEFAULT_OPTIONS, **self._device.entry.options}
            tone = options[CONF_ALERT_TONE if level == "alert" else CONF_WARNING_TONE]
        image_url = None
        if camera:
            image_url = self._device.media_url(f"/api/camera_proxy/{camera}")
        elif image:
            if media_source.is_media_source_id(image):
                image = (await media_source.async_resolve_media(self.hass, image, self.entity_id)).url
            image_url = self._device.media_url(image)
        await self._device.async_command(
            "notify",
            message=message,
            title=title,
            image_url=image_url,
            duration=duration,
            position=position,
            level=level,
            tone=tone if level != "notice" else None,
        )

    async def async_home(self) -> None:
        """Close everything and return to the home dashboard."""
        await self._device.async_command("home")

    async def async_reload_dashboard(self) -> None:
        """Reload the dashboard page."""
        await self._device.async_command("reload")

    async def async_forget_sign_ins(self, site: str | None = None) -> None:
        """Forget the sign-ins the TV saved for dashboards: for one site, or all."""
        await self._device.async_command("forget_sign_ins", site=site)

