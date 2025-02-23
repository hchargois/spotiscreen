import os
import json
import time
from dataclasses import dataclass
import requests
from io import BytesIO
from functools import lru_cache
import signal

from PIL import Image
import spotipy
import xdg
from smartscreen_driver.lcd_comm_rev_a import LcdCommRevA, Orientation
from smartscreen_driver.lcd_simulated import LcdSimulated
from pidili import Pidili
from pidili.widgets import Widget, Text, Img, Rect, ProgressBar

from spotiscreen.ticker import ticker


def main():
    app_config_dir = xdg.xdg_config_home() / "spotiscreen"
    os.makedirs(app_config_dir, exist_ok=True)

    config_file = app_config_dir / "config.json"
    token_file = app_config_dir / "token.json"

    # Load or create config file
    if config_file.exists():
        with open(config_file) as f:
            config = json.load(f)
        client_id = config["client_id"]
    else:
        print(f"No config file found, creating one in {config_file}")
        client_id = input("Please enter your Spotify app's client ID: ").strip()
        config = {"client_id": client_id}
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)

    scope = "user-read-playback-state"
    credentials_manager = spotipy.oauth2.SpotifyPKCE(
        scope=scope,
        client_id=client_id,
        redirect_uri="http://localhost:3000",
        cache_handler=spotipy.cache_handler.CacheFileHandler(
            cache_path=str(token_file)
        ),
    )
    spot = spotipy.Spotify(client_credentials_manager=credentials_manager)

    run(spot)


class Screen:
    # TODO integrate the Pidili in this class
    def __init__(self, simulated: bool = False):
        self.lcd = LcdCommRevA() if not simulated else LcdSimulated()
        self.lcd.reset()
        self.lcd.initialize_comm()
        self.lcd.set_brightness(25)
        self.lcd.set_orientation(Orientation.LANDSCAPE)

        self.is_on = True

    def paint(self, img: Image.Image, pos: tuple[int, int]):
        self.lcd.paint(img, pos)

    def off(self):
        if not self.is_on:
            return
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


@lru_cache(maxsize=32)
def download_image(url: str) -> Image.Image:
    response = requests.get(url)
    response.raise_for_status()
    return Image.open(BytesIO(response.content))


@dataclass
class NowPlayingState:
    # TODO remove the response parsing from this class (?) or at least move it to a "from_response" method
    artist: str
    track_name: str
    album: str
    album_art_url: str
    album_art_img: Image.Image | None
    progress_seconds: int
    duration_seconds: int
    track_number: int
    total_tracks: int

    def __init__(self, api_resp: dict):
        self.artist = api_resp["item"]["artists"][0]["name"]
        self.track_name = api_resp["item"]["name"]
        self.album = api_resp["item"]["album"]["name"]
        self.album_art_url = api_resp["item"]["album"]["images"][0]["url"]
        self.progress_seconds = api_resp["progress_ms"] / 1000
        self.duration_seconds = api_resp["item"]["duration_ms"] / 1000
        self.track_number = api_resp["item"]["track_number"]
        self.total_tracks = api_resp["item"]["album"]["total_tracks"]
        try:
            self.album_art_img = download_image(self.album_art_url)
        except Exception as e:
            print(f"Error downloading album art: {e}")
            self.album_art_img = None

    def progress_percent(self) -> float:
        return self.progress_seconds / self.duration_seconds * 100


def seconds_to_min_secs(seconds: int) -> str:
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes}:{seconds:02d}"


font = "DejaVuSans.ttf"


# TODO move this to a dedicated module
def build_scene(size: tuple[int, int], state: NowPlayingState) -> Widget:
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
            seconds_to_min_secs(int(state.progress_seconds)),
            color=(200, 200, 200),
            font=font,
            font_size=20,
        ),
    )
    scene.add(
        (475, 290),
        Text(
            seconds_to_min_secs(int(state.duration_seconds)),
            color=(200, 200, 200),
            font=font,
            font_size=20,
            anchor="ra",
        ),
    )
    scene.add(
        (240, 290),
        Text(
            f"{state.track_number} / {state.total_tracks}",
            color=(200, 200, 200),
            font=font,
            font_size=20,
            anchor="ma",
        ),
    )
    album = Text(
        state.album,
        color=(200, 200, 200),
        font=font,
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
            font=font,
            font_size=24,
            max_width=215,
        ),
    )
    scene.add(
        (265, 255),
        Text(
            state.track_name,
            font=font,
            font_size=20,
            max_width=215,
            anchor="ld",
            color=(255, 255, 255),
        ),
    )
    return scene


def run(spot: spotipy.Spotify):
    running = True
    screen = None

    def signal_handler(signum, frame):
        nonlocal running
        print(f"\nReceived signal {signum}, shutting down...")
        running = False

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while running:
        try:
            screen = Screen(simulated=False)
            pdl = Pidili(screen.paint)

            for _ in ticker(1):
                if not running:
                    break
                current_playback = spot.current_playback()
                if current_playback is None:
                    screen.off()
                    pdl.reset()
                    time.sleep(5)
                    continue
                screen.on()
                now_playing_state = NowPlayingState(current_playback)
                scene = build_scene(screen.size(), now_playing_state)
                pdl.update(scene)

        except Exception as e:
            print(f"Error occurred: {e}")
            print("Retrying in 5 seconds...")
            if running:
                time.sleep(5)

    # Clean up resources
    if screen:
        screen.off()
    print("Shutdown complete")


if __name__ == "__main__":
    main()
