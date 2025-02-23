import time
from typing import Generator


def ticker(interval: float) -> Generator[None, None, None]:
    next_tick = time.time()
    while True:
        now = time.time()
        if now >= next_tick:
            next_tick = max(next_tick + interval, now + interval)
            yield
        else:
            time.sleep(next_tick - now)
