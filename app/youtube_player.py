from selenium.webdriver.support.ui import WebDriverWait

from .config import HOST, PORT


def open_player(driver, video_id, stopped):
    driver.get(f"http://{HOST}:{PORT}/youtube-player?video_id={video_id}")

    def ready(item):
        if stopped.is_set():
            return True
        state = item.execute_script(
            "return window.youtubeDiagnostic ? window.youtubeDiagnostic.snapshot() : null;"
        )
        if state and state.get("error"):
            raise RuntimeError(state["error"])
        return bool(state and state.get("ready"))

    WebDriverWait(driver, 20).until(
        ready, message="YouTube player did not become ready within 20 seconds."
    )


def read_player(driver):
    return driver.execute_script("return window.youtubeDiagnostic.snapshot();")
