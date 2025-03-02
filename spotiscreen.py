import os
import json
import time
from dataclasses import dataclass, asdict
from io import BytesIO
from functools import lru_cache
import signal
from typing import Generator
import webbrowser
import logging
import inspect

from PIL import Image
import spotipy
import xdg
import requests
from loguru import logger
from smartscreen_driver.lcd_comm_rev_a import LcdCommRevA, Orientation
from smartscreen_driver.lcd_simulated import LcdSimulated
from pidili import Pidili
from pidili.widgets import Widget, Text, Img, Rect, ProgressBar


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists.
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message.
        frame, depth = inspect.currentframe(), 0
        while frame:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)


@dataclass
class Config:
    client_id: str = ""
    redirect_uri: str = ""
    font: str = "DejaVuSans.ttf"
    brightness: int = 25
    simulated: bool = False

    @classmethod
    def load(cls, path: os.PathLike) -> "Config":
        try:
            with open(path) as f:
                return cls(**json.load(f))
        except FileNotFoundError:
            logger.info(f"No config file found, creating one in {path}")
            return cls()
        except Exception as e:
            logger.error(f"Error loading config, falling back to defaults: {e}")
            return cls()

    def save(self, path: os.PathLike):
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)


def main():
    app_config_dir = xdg.xdg_config_home() / "spotiscreen"
    os.makedirs(app_config_dir, exist_ok=True)

    config_file = app_config_dir / "config.json"
    token_file = app_config_dir / "token.json"

    cfg = Config.load(config_file)
    if not cfg.client_id:
        cfg.client_id = input("Please enter your Spotify app's client ID: ").strip()
    if not cfg.redirect_uri:
        cfg.redirect_uri = input(
            "Please enter your Spotify app's redirect URI: "
        ).strip()
    cfg.save(config_file)

    scope = "user-read-playback-state"
    credentials_manager = spotipy.oauth2.SpotifyPKCE(
        scope=scope,
        client_id=cfg.client_id,
        redirect_uri=cfg.redirect_uri,
        cache_handler=spotipy.cache_handler.CacheFileHandler(
            cache_path=str(token_file)
        ),
    )
    spot = spotipy.Spotify(client_credentials_manager=credentials_manager)
    # spotipy doesn't actually try to connect until an API call is made, so
    # let's do a call just to make sure everything works and run the auth flow
    # if needed, before trying to connect to the screen
    spot.current_playback()

    run(cfg, spot)


class Screen:
    def __init__(self, cfg: Config):
        if cfg.simulated:
            self.lcd = LcdSimulated()
            host, port = self.lcd.webServer.server_address
            url = f"http://{host}:{port}"
            logger.info(f"Simulated LCD running at {url}")
            webbrowser.open(url)
        else:
            self.lcd = LcdCommRevA()
        self.lcd.reset()
        self.lcd.initialize_comm()
        self.lcd.set_brightness(cfg.brightness)
        self.lcd.set_orientation(Orientation.LANDSCAPE)

        self.is_on = True
        self.pdl = Pidili(self.lcd.paint)

    def off(self):
        if not self.is_on:
            return
        self.pdl.reset()
        self.lcd.screen_off()
        self.lcd.clear()
        self.is_on = False

    def on(self):
        if self.is_on:
            return
        self.lcd.screen_on()
        self.is_on = True

    def size(self) -> tuple[int, int]:
        return self.lcd.size()

    def update(self, scene: Widget):
        self.pdl.update(scene)


@lru_cache(maxsize=32)
def download_image(url: str) -> Image.Image:
    response = requests.get(url)
    response.raise_for_status()
    return Image.open(BytesIO(response.content))


@dataclass
class NowPlayingState:
    artist: str
    track_name: str
    album: str
    album_art_url: str
    album_art_img: Image.Image | None
    progress_ms: int
    duration_ms: int
    track_number: int
    total_tracks: int

    @classmethod
    def from_api_response(cls, api_resp: dict) -> "NowPlayingState":
        album_art_url = api_resp["item"]["album"]["images"][0]["url"]
        try:
            album_art_img = download_image(album_art_url)
        except Exception as e:
            logger.warning(f"Error downloading album art: {e}")
            album_art_img = None

        return cls(
            artist=api_resp["item"]["artists"][0]["name"],
            track_name=api_resp["item"]["name"],
            album=api_resp["item"]["album"]["name"],
            progress_ms=api_resp["progress_ms"],
            duration_ms=api_resp["item"]["duration_ms"],
            track_number=api_resp["item"]["track_number"],
            total_tracks=api_resp["item"]["album"]["total_tracks"],
            album_art_url=album_art_url,
            album_art_img=album_art_img,
        )

    def progress_percent(self) -> float:
        return self.progress_ms / self.duration_ms * 100


def ms_to_min_secs(ms: int) -> str:
    seconds = ms // 1000
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes}:{seconds:02d}"


def ticker(interval: float) -> Generator[None, None, None]:
    next_tick = time.time() + interval
    while True:
        yield
        now = time.time()
        if now > next_tick:
            next_tick = now + interval
        else:
            time.sleep(next_tick - now)
            next_tick += interval


def build_scene(cfg: Config, size: tuple[int, int], state: NowPlayingState) -> Widget:
    scene = Rect(size, fill=(0, 0, 0))
    if state.album_art_img:
        scene.add(
            (0, 0),
            Img((255, 255), state.album_art_img, key=state.album_art_url),
        )
    scene.add(
        (0, 270),
        ProgressBar((480, 5), state.progress_percent(), fill=(64, 64, 64)),
    )
    scene.add(
        (5, 290),
        Text(
            ms_to_min_secs(int(state.progress_ms)),
            color=(200, 200, 200),
            font=cfg.font,
            font_size=20,
        ),
    )
    scene.add(
        (475, 290),
        Text(
            ms_to_min_secs(int(state.duration_ms)),
            color=(200, 200, 200),
            font=cfg.font,
            font_size=20,
            anchor="ra",
        ),
    )
    scene.add(
        (240, 290),
        Text(
            f"{state.track_number} / {state.total_tracks}",
            color=(200, 200, 200),
            font=cfg.font,
            font_size=20,
            anchor="ma",
        ),
    )
    album = Text(
        state.album,
        color=(200, 200, 200),
        font=cfg.font,
        font_size=16,
        max_width=215,
    )

    scene.add(
        (265, 2),
        album,
    )
    scene.add(
        (265, album.height + 20),
        Text(
            state.artist,
            color=(255, 255, 255),
            font=cfg.font,
            font_size=24,
            max_width=215,
        ),
    )
    scene.add(
        (265, 255),
        Text(
            state.track_name,
            font=cfg.font,
            font_size=20,
            max_width=215,
            anchor="ld",
            color=(255, 255, 255),
        ),
    )
    return scene


def run(cfg: Config, spot: spotipy.Spotify):
    logger.info("Starting")
    running = True
    screen = None

    def signal_handler(signum, frame):
        nonlocal running
        logger.info(f"Received signal {signum}, shutting down...")
        running = False

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while running:
        try:
            screen = Screen(cfg)
            logger.info("Screen initialized, now running...")

            for _ in ticker(1):
                if not running:
                    break
                current_playback = spot.current_playback()
                if current_playback is None:
                    screen.off()
                    time.sleep(5)  # poll less often when nothing is playing
                    continue
                screen.on()
                now_playing_state = NowPlayingState.from_api_response(current_playback)
                scene = build_scene(cfg, screen.size(), now_playing_state)
                screen.update(scene)

        except Exception as e:
            logger.error(f"Error occurred: {e}")
            logger.info("Retrying in 5 seconds...")
            if running:
                time.sleep(5)

    # Clean up resources
    if screen:
        screen.off()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
