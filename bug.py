import base64
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from seleniumbase import SB

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("viewer_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    # Encoded channel name (base64)
    encoded_name: str = "YnJ1dGFsbGVz"

    # Proxy — set to a "host:port" string or None for direct connection
    proxy: Optional[str] = None

    # How many viewer sessions to spawn per cycle (1 = primary only, 2 = primary + secondary)
    viewer_count: int = 2

    # Watch duration range in seconds
    watch_min: int = 450
    watch_max: int = 800

    # Startup sleep after navigating (seconds)
    nav_sleep: int = 12
    action_sleep: int = 10
    short_sleep: int = 2

    # Maximum consecutive retries on failure before exiting
    max_retries: int = 1

    # Chromium flags
    chromium_args: list = field(default_factory=lambda: ["--disable-webgl"])

    @property
    def channel_name(self) -> str:
        return base64.b64decode(self.encoded_name).decode("utf-8")

    @property
    def channel_url(self) -> str:
        return f"https://www.twitch.tv/{self.channel_name}"


# ---------------------------------------------------------------------------
# Geo data
# ---------------------------------------------------------------------------

@dataclass
class GeoData:
    latitude: float
    longitude: float
    timezone_id: str
    language_code: str

    @classmethod
    def fetch(cls, timeout: int = 10, retries: int = 3) -> "GeoData":
        for attempt in range(1, retries + 1):
            try:
                log.info("Fetching geo data (attempt %d/%d)...", attempt, retries)
                resp = requests.get("http://ip-api.com/json/", timeout=timeout)
                resp.raise_for_status()
                data = resp.json()

                if data.get("status") != "success":
                    raise ValueError(f"ip-api returned non-success status: {data}")

                return cls(
                    latitude=data["lat"],
                    longitude=data["lon"],
                    timezone_id=data["timezone"],
                    language_code=data["countryCode"].lower(),
                )
            except Exception as exc:
                log.warning("Geo fetch failed: %s", exc)
                if attempt < retries:
                    time.sleep(2 ** attempt)  # exponential back-off
        raise RuntimeError("Could not fetch geo data after multiple attempts.")


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def _accept_cookies(driver, timeout: int = 4) -> None:
    """Click any visible Accept / consent button."""
    for selector in [
        'button:contains("Accept")',
        'button:contains("Accept All")',
        'button:contains("I Agree")',
    ]:
        try:
            if driver.is_element_present(selector):
                driver.cdp.click(selector, timeout=timeout)
                log.debug("Clicked consent button: %s", selector)
                return
        except Exception:
            pass


def _click_start_watching(driver, timeout: int = 4) -> bool:
    """Click 'Start Watching' if present. Returns True if clicked."""
    selector = 'button:contains("Start Watching")'
    try:
        if driver.is_element_present(selector):
            driver.cdp.click(selector, timeout=timeout)
            log.info("Clicked 'Start Watching'.")
            return True
    except Exception as exc:
        log.warning("Could not click 'Start Watching': %s", exc)
    return False


def _is_stream_live(driver) -> bool:
    return driver.is_element_present("#live-channel-stream-information")


def _open_viewer_session(
    parent_driver,
    url: str,
    geo: GeoData,
    cfg: Config,
    label: str = "secondary",
) -> object:
    """Spawn and initialise an additional viewer window."""
    log.info("Opening %s viewer session...", label)
    driver = parent_driver.get_new_driver(undetectable=True)
    driver.activate_cdp_mode(
        url,
        tzone=geo.timezone_id,
        geoloc=(geo.latitude, geo.longitude),
    )
    driver.sleep(cfg.action_sleep)
    if _click_start_watching(driver):
        driver.sleep(cfg.action_sleep)
    _accept_cookies(driver)
    log.info("%s viewer session ready.", label.capitalize())
    return driver


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

def run_cycle(cfg: Config, geo: GeoData) -> bool:
    """
    Execute one full watch cycle.
    Returns True if the cycle completed normally, False if the stream was offline.
    """
    watch_duration = random.randint(cfg.watch_min, cfg.watch_max)
    log.info(
        "Starting cycle | channel=%s | duration=%ds | proxy=%s",
        cfg.channel_name,
        watch_duration,
        cfg.proxy or "direct",
    )

    chromium_arg = " ".join(cfg.chromium_args)

    with SB(
        uc=True,
        locale="en",
        ad_block=True,
        chromium_arg=chromium_arg,
        proxy=cfg.proxy,
    ) as primary:

        # --- Navigate ---
        primary.activate_cdp_mode(
            cfg.channel_url,
            tzone=geo.timezone_id,
            geoloc=(geo.latitude, geo.longitude),
        )
        primary.sleep(cfg.short_sleep)
        _accept_cookies(primary)
        primary.sleep(cfg.short_sleep)

        # --- Wait for page to settle ---
        primary.sleep(cfg.nav_sleep)

        # --- Start watching prompt ---
        if _click_start_watching(primary):
            primary.sleep(cfg.action_sleep)
        _accept_cookies(primary)

        # --- Check live status ---
        if not _is_stream_live(primary):
            log.warning("Stream does not appear to be live. Aborting cycle.")
            return False

        log.info("Stream is LIVE.")
        _accept_cookies(primary)

        # --- Additional viewer sessions ---
        extra_drivers = []
        for idx in range(2, cfg.viewer_count + 1):
            try:
                drv = _open_viewer_session(
                    primary,
                    cfg.channel_url,
                    geo,
                    cfg,
                    label=f"viewer-{idx}",
                )
                extra_drivers.append(drv)
            except Exception as exc:
                log.error("Failed to open extra viewer session: %s", exc)

        # --- Watch ---
        log.info("Watching for %d seconds...", watch_duration)
        primary.sleep(watch_duration)

    log.info("Cycle completed successfully.")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = Config()
    geo = GeoData.fetch()

    log.info(
        "Bot initialised | channel=%s | lat=%.4f | lon=%.4f | tz=%s",
        cfg.channel_name,
        geo.latitude,
        geo.longitude,
        geo.timezone_id,
    )

    consecutive_failures = 0

    while True:
        try:
            success = run_cycle(cfg, geo)
            if success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                log.warning(
                    "Offline cycle (%d/%d). Waiting 60s before retry...",
                    consecutive_failures,
                    cfg.max_retries,
                )
                time.sleep(60)

        except KeyboardInterrupt:
            log.info("Interrupted by user. Exiting.")
            break

        except Exception as exc:
            consecutive_failures += 1
            log.exception("Unexpected error in cycle: %s", exc)
            backoff = min(30 * consecutive_failures, 300)
            log.info("Backing off for %ds...", backoff)
            time.sleep(backoff)

        if consecutive_failures >= cfg.max_retries:
            sys.exit(1)


if __name__ == "__main__":
    main()
