# -*- coding: utf-8 -*-
"""
URL detection tool.

- Opens a browser at startup with:
  https://shop.kitamura.jp/ec/list?type=u&limit=100&page=1&sort=update_date
- When page URL changes and includes:
  https://shop.kitamura.jp/ec/used/
  shows notification: "The page has changed."
"""
import os
import sys
import threading
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from playwright.sync_api import sync_playwright

    BROWSER_AVAILABLE = True
except Exception:
    BROWSER_AVAILABLE = False

START_URL = "https://shop.kitamura.jp/ec/list?type=u&limit=100&page=1&sort=update_date"
TARGET_URL_PART = "https://shop.kitamura.jp/ec/used/"

URL_WATCHER_SCRIPT = """
(() => {
    if (window.__urlDetectionStarted) return;
    window.__urlDetectionStarted = true;

    let lastHref = window.location.href;

    const notifyChange = () => {
        const current = window.location.href;
        if (current === lastHref) return;
        lastHref = current;
        if (window.notifyUrlChange) {
            window.notifyUrlChange(current);
        }
    };

    const wrapHistory = (methodName) => {
        const original = history[methodName];
        if (!original) return;
        history[methodName] = function(...args) {
            const result = original.apply(this, args);
            notifyChange();
            return result;
        };
    };

    wrapHistory("pushState");
    wrapHistory("replaceState");
    window.addEventListener("popstate", notifyChange, true);
    window.addEventListener("hashchange", notifyChange, true);
    setInterval(notifyChange, 200);
})();
"""


class SignalEmitter(QObject):
    log_signal = pyqtSignal(str)
    target_url_changed_signal = pyqtSignal(str)
    browser_stopped_signal = pyqtSignal()


class UrlDetectionWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("URL Detection Tool")
        self.resize(760, 420)

        self.signal_emitter = SignalEmitter()
        self.signal_emitter.log_signal.connect(self.add_log)
        self.signal_emitter.target_url_changed_signal.connect(self.on_target_url_changed)
        self.signal_emitter.browser_stopped_signal.connect(self.on_browser_stopped)

        self.playwright = None
        self.browser_context = None
        self.main_page = None
        self.browser_thread = None
        self.browser_running = False

        self.last_seen_urls = {}
        self.tracked_pages = []
        self.binding_registered_pages = set()
        self.last_notified_url = None

        self._setup_ui()
        QTimer.singleShot(300, self.on_start)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        title = QLabel("URL Detection")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        root.addWidget(title)

        self.status_label = QLabel("Status: Idle")
        self.status_label.setStyleSheet("color: #333;")
        root.addWidget(self.status_label)

        self.target_label = QLabel(f"Target URL part: {TARGET_URL_PART}")
        self.target_label.setStyleSheet("color: #333;")
        root.addWidget(self.target_label)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.on_start)
        button_row.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.on_stop)
        self.stop_button.setEnabled(False)
        button_row.addWidget(self.stop_button)
        root.addLayout(button_row)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        root.addWidget(self.log_view)

    def add_log(self, message: str) -> None:
        self.log_view.append(message)

    def on_start(self) -> None:
        if not BROWSER_AVAILABLE:
            self.add_log("Playwright is not available. Run: pip install -r requirements.txt")
            return
        if self.browser_running:
            self.add_log("Browser is already running.")
            return

        self.browser_running = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status_label.setText("Status: Running")
        self.add_log("Starting browser...")

        self.browser_thread = threading.Thread(target=self._browser_loop, daemon=True)
        self.browser_thread.start()

    def on_stop(self) -> None:
        if not self.browser_running:
            return
        self.add_log("Stopping browser...")
        self.browser_running = False

    def _launch_persistent_context(self, profile_path: str):
        launch_kwargs = {
            "user_data_dir": profile_path,
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            return self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            try:
                return self.playwright.chromium.launch_persistent_context(channel="msedge", **launch_kwargs)
            except Exception:
                return self.playwright.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)

    @staticmethod
    def _page_key(page) -> str:
        try:
            return page._impl_obj._guid  # stable key for same underlying Playwright page
        except Exception:
            return str(id(page))

    def _register_page(self, page) -> None:
        if page in self.tracked_pages:
            return
        self.tracked_pages.append(page)
        page_key = self._page_key(page)
        if page_key not in self.last_seen_urls:
            self.last_seen_urls[page_key] = page.url or ""

        def on_framenavigated(frame):
            try:
                if frame != page.main_frame:
                    return
                self._handle_url_update(page, frame.url or "")
            except Exception:
                pass

        try:
            page.on("framenavigated", on_framenavigated)
        except Exception:
            pass

    def _handle_url_update(self, page, href: str) -> None:
        if not self.browser_running or not href:
            return
        page_key = self._page_key(page)
        previous = self.last_seen_urls.get(page_key)
        if href == previous:
            return

        self.last_seen_urls[page_key] = href
        self.signal_emitter.log_signal.emit(f"URL changed: {href}")
        if TARGET_URL_PART in href:
            self.signal_emitter.target_url_changed_signal.emit(href)

    def _inject_url_watcher(self, page) -> None:
        if page.is_closed():
            return
        page_key = self._page_key(page)
        if page_key not in self.binding_registered_pages:
            page.expose_binding("notifyUrlChange", self._on_url_changed)
            self.binding_registered_pages.add(page_key)
        page.evaluate(URL_WATCHER_SCRIPT)
        if page_key not in self.last_seen_urls:
            self.last_seen_urls[page_key] = page.url or ""

    def _on_url_changed(self, source, href: str) -> None:
        page = getattr(source, "page", None)
        if not page:
            return
        self._handle_url_update(page, href)

    def _browser_loop(self) -> None:
        try:
            profile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profile")
            os.makedirs(profile_path, exist_ok=True)

            self.playwright = sync_playwright().start()
            self.browser_context = self._launch_persistent_context(profile_path)

            try:
                self.browser_context.add_init_script(URL_WATCHER_SCRIPT)
            except Exception as exc:
                self.signal_emitter.log_signal.emit(f"Failed to add init script: {exc}")

            if self.browser_context.pages:
                self.main_page = self.browser_context.pages[0]
            else:
                self.main_page = self.browser_context.new_page()

            self.main_page.set_default_timeout(60000)
            self._register_page(self.main_page)

            self.signal_emitter.log_signal.emit(f"Opening: {START_URL}")
            self.main_page.goto(START_URL, wait_until="domcontentloaded")

            # Register/inject watcher for all existing tabs in this context.
            for page in list(self.browser_context.pages):
                try:
                    page.set_default_timeout(60000)
                except Exception:
                    pass
                self._register_page(page)
                self._inject_url_watcher(page)

            # Baseline URLs first, then begin change notifications.
            for page in self.tracked_pages:
                if page.is_closed():
                    continue
                self.last_seen_urls[self._page_key(page)] = page.url or ""

            def on_new_page(page) -> None:
                try:
                    page.set_default_timeout(60000)
                    self._register_page(page)
                    self._inject_url_watcher(page)
                    self.signal_emitter.log_signal.emit("New tab detected.")
                except Exception as exc:
                    self.signal_emitter.log_signal.emit(f"Failed to watch new tab: {exc}")

            self.browser_context.on("page", on_new_page)
            self.signal_emitter.log_signal.emit("URL watcher is active.")

            while self.browser_running and self.browser_context:
                for page in list(self.browser_context.pages):
                    if page.is_closed():
                        continue

                    self._register_page(page)
                    page_key = self._page_key(page)
                    if page_key not in self.binding_registered_pages:
                        self._inject_url_watcher(page)

                    current_url = ""
                    try:
                        current_url = page.evaluate(
                            "() => (window && window.location && window.location.href) ? window.location.href : ''"
                        ) or ""
                    except Exception:
                        try:
                            current_url = page.url or ""
                        except Exception:
                            current_url = ""

                    self._handle_url_update(page, current_url)

                time.sleep(0.25)

        except Exception as exc:
            self.signal_emitter.log_signal.emit(f"Browser loop error: {exc}")
        finally:
            try:
                if self.browser_context:
                    self.browser_context.close()
            except Exception:
                pass
            try:
                if self.playwright:
                    self.playwright.stop()
            except Exception:
                pass

            self.playwright = None
            self.browser_context = None
            self.main_page = None
            self.browser_running = False
            self.last_seen_urls.clear()
            self.tracked_pages.clear()
            self.binding_registered_pages.clear()
            self.signal_emitter.browser_stopped_signal.emit()

    def on_target_url_changed(self, url: str) -> None:
        if url == self.last_notified_url:
            return
        self.last_notified_url = url
        self.add_log(f"Target URL detected: {url}")
        QMessageBox.information(self, "Notification", "The page has changed.")

    def on_browser_stopped(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText("Status: Stopped")
        self.add_log("Browser stopped.")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self.on_stop()
        if self.browser_thread and self.browser_thread.is_alive():
            self.browser_thread.join(timeout=3)
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    window = UrlDetectionWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
