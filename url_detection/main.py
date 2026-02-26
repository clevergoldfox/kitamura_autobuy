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
import queue
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

PURCHASE_SCRIPT = """
async () => {
    const safeGet = (getter) => {
        if (typeof getter !== "function") return null;
        try {
            return getter() || null;
        } catch {
            return null;
        }
    };

    const waitForElement = (getter, timeout = 3500, interval = 100) => new Promise((resolve) => {
        const deadline = Date.now() + timeout;
        const timerId = setInterval(() => {
            const el = safeGet(getter);
            if (el) {
                clearInterval(timerId);
                resolve(el);
                return;
            }
            if (Date.now() >= deadline) {
                clearInterval(timerId);
                resolve(null);
            }
        }, interval);
    });

    const waitForUrlContains = (keyword, timeout = 6000) => new Promise((resolve) => {
        const deadline = Date.now() + timeout;
        const timerId = setInterval(() => {
            if ((window.location.href || "").includes(keyword)) {
                clearInterval(timerId);
                resolve(true);
                return;
            }
            if (Date.now() >= deadline) {
                clearInterval(timerId);
                resolve(false);
            }
        }, 100);
    });

    const fastClick = (xpaths, timeout = 3500) => {
        const targets = Array.isArray(xpaths) ? xpaths : [xpaths];
        return new Promise((resolve, reject) => {
            const start = Date.now();
            let clicked = false;

            const tryClick = () => {
                if (clicked) return true;
                for (const xpath of targets) {
                    const node = document.evaluate(
                        xpath,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null
                    ).singleNodeValue;
                    if (node && node.offsetParent !== null) {
                        clicked = true;
                        node.click();
                        resolve(true);
                        return true;
                    }
                }
                return false;
            };

            if (tryClick()) return;

            const timerId = setInterval(() => {
                if (tryClick()) {
                    clearInterval(timerId);
                    return;
                }
                if (Date.now() - start > timeout) {
                    clearInterval(timerId);
                    reject(new Error("click_timeout"));
                }
            }, 80);
        });
    };

    if (document.querySelector("span.not-sales-text")) {
        return { error: "not_available" };
    }

    try {
        await fastClick(
            "//button[contains(@class,'cart-button') and .//i[contains(@class,'cart-button-icon') and contains(@class,'fa-shopping-cart')]]"
        );

        try {
            await fastClick(
                [
                    "//div[contains(@class,'cart-dialog') or contains(@class,'cart-dialog-basic')]//button[contains(@class,'cart-button') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'予約する')]]",
                    "//div[contains(@class,'cart-dialog') or contains(@class,'cart-dialog-basic')]//button[contains(normalize-space(),'購入手続きへ進む')]",
                ],
                900
            );
        } catch {}

        await fastClick(
            "//button[contains(@class,'action-btn') and contains(@class,'action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'購入手続きへ進む')]]"
        );

        const movedToCart = await waitForUrlContains("shop.kitamura.jp/ec/cart", 7000);
        if (!movedToCart) return { error: "cart_timeout" };

        await fastClick(
            "//button[contains(@class,'v-btn--block') and contains(@class,'action-btn') and contains(@class,'action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'購入手続きへ進む')]]"
        );

        const movedToOrder = await waitForUrlContains("shop.kitamura.jp/ec/order", 7000);
        if (!movedToOrder) return { error: "order_timeout" };

        const checkbox = await waitForElement(() => {
            const el = document.querySelector("#order-receive-shop");
            if (!el || el.type !== "checkbox" || el.offsetParent === null) return null;
            return el;
        });
        if (!checkbox) return { error: "checkbox_not_found" };

        checkbox.click();
        if (!checkbox.checked) checkbox.click();

        await fastClick(
            "//button[contains(@class,'order-action-btn') and contains(@class,'order-action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'注文内容確認へ進む')]]"
        );

        try {
            await fastClick(
                "//button[contains(@class,'error-dialog-btn') and .//span[contains(@class,'v-btn__content') and normalize-space()='OK']]",
                900
            );
        } catch {}

        await fastClick(
            "//button[contains(@class,'order-action-btn') and contains(@class,'order-action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'注文確定')]]"
        );

        return { success: true };
    } catch (e) {
        return { error: e && e.message ? e.message : "unknown_error" };
    }
}
"""


class SignalEmitter(QObject):
    log_signal = pyqtSignal(str)
    target_url_changed_signal = pyqtSignal(str)
    purchase_result_signal = pyqtSignal(bool, str)
    browser_stopped_signal = pyqtSignal()


class UrlDetectionWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("URL検知ツール")
        self.resize(760, 420)

        self.signal_emitter = SignalEmitter()
        self.signal_emitter.log_signal.connect(self.add_log)
        self.signal_emitter.target_url_changed_signal.connect(self.on_target_url_changed)
        self.signal_emitter.purchase_result_signal.connect(self.on_purchase_result)
        self.signal_emitter.browser_stopped_signal.connect(self.on_browser_stopped)

        self.playwright = None
        self.browser_context = None
        self.main_page = None
        self.browser_thread = None
        self.browser_running = False

        self.last_seen_urls = {}
        self.tracked_pages = []
        self.binding_registered_pages = set()
        self.confirm_pending = False
        self.command_queue = queue.Queue()

        self._setup_ui()
        QTimer.singleShot(300, self.on_start)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        title = QLabel("URL検知")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        root.addWidget(title)

        self.status_label = QLabel("状態: 停止中")
        self.status_label.setStyleSheet("color: #333;")
        root.addWidget(self.status_label)

        self.target_label = QLabel(f"検知対象URL: {TARGET_URL_PART}")
        self.target_label.setStyleSheet("color: #333;")
        root.addWidget(self.target_label)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("開始")
        self.start_button.clicked.connect(self.on_start)
        button_row.addWidget(self.start_button)

        self.stop_button = QPushButton("停止")
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
            self.add_log("Playwright が利用できません。`pip install -r requirements.txt` を実行してください。")
            return
        if self.browser_running:
            self.add_log("ブラウザはすでに起動しています。")
            return

        self.browser_running = True
        self.confirm_pending = False
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status_label.setText("状態: 実行中")
        self.add_log("ブラウザを起動します...")

        self.browser_thread = threading.Thread(target=self._browser_loop, daemon=True)
        self.browser_thread.start()

    def on_stop(self) -> None:
        if not self.browser_running:
            return
        self.add_log("ブラウザを停止します...")
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
        self.signal_emitter.log_signal.emit(f"URL変更を検知: {href}")
        if TARGET_URL_PART in href and not self.confirm_pending:
            self.confirm_pending = True
            self.signal_emitter.target_url_changed_signal.emit(href)

    def _show_login_modal_if_needed(self) -> None:
        if not self.main_page:
            return
        try:
            html = self.main_page.content()
            has_login_modal = (
                "あなたのアカウントにログイン" in html
                or ("ログイン" in html and "パスワード" in html and "新規会員登録" in html)
            )
            looks_logged_in = (
                "会員情報" in html or "マイページ" in html or "ログアウト" in html
            )

            if looks_logged_in:
                self.signal_emitter.log_signal.emit("ログイン済みのため、そのまま監視を開始します。")
                return

            if has_login_modal:
                self.signal_emitter.log_signal.emit("未ログインです。ログインモーダルが表示されています。")
                return

            clicked = self.main_page.evaluate(
                """
                () => {
                    const nodes = document.querySelectorAll("a, button, span[role='button'], div[role='button']");
                    for (const node of nodes) {
                        const text = (node.textContent || "").trim();
                        if (text === "ログイン" || text === "ログインする") {
                            node.click();
                            return true;
                        }
                    }
                    return false;
                }
                """
            )
            if clicked:
                self.signal_emitter.log_signal.emit("未ログインのため、ログインモーダルを表示しました。")
            else:
                self.signal_emitter.log_signal.emit("ログインボタンが見つかりません。手動でログインしてください。")
        except Exception as exc:
            self.signal_emitter.log_signal.emit(f"ログイン状態の確認中にエラー: {exc}")

    def _go_start_url(self) -> None:
        if not self.main_page or self.main_page.is_closed():
            return
        try:
            self.main_page.goto(START_URL, wait_until="domcontentloaded")
            self.signal_emitter.log_signal.emit("開始URLに戻りました。")
        except Exception as exc:
            self.signal_emitter.log_signal.emit(f"開始URLへの遷移に失敗: {exc}")

    def _find_page_by_url(self, target_url: str):
        if not self.browser_context:
            return None

        target_url = (target_url or "").strip()
        pages = list(self.browser_context.pages)
        for page in pages:
            if page.is_closed():
                continue
            current = ""
            try:
                current = page.evaluate(
                    "() => (window && window.location && window.location.href) ? window.location.href : ''"
                ) or ""
            except Exception:
                try:
                    current = page.url or ""
                except Exception:
                    current = ""
            if current == target_url:
                return page

        for page in pages:
            if page.is_closed():
                continue
            try:
                current = page.url or ""
            except Exception:
                current = ""
            if TARGET_URL_PART in current:
                return page
        return self.main_page

    def _execute_purchase(self, target_url: str) -> None:
        success = False
        err_msg = ""
        try:
            page = self._find_page_by_url(target_url)
            if not page or page.is_closed():
                raise RuntimeError("購入対象ページが見つかりません。")

            self.signal_emitter.log_signal.emit("購入処理を開始します...")
            result = page.evaluate(PURCHASE_SCRIPT)

            if isinstance(result, dict) and result.get("success"):
                success = True
            else:
                code = result.get("error") if isinstance(result, dict) else "unknown_error"
                error_map = {
                    "not_available": "商品は購入できない状態です。",
                    "cart_timeout": "カート画面への遷移がタイムアウトしました。",
                    "order_timeout": "注文画面への遷移がタイムアウトしました。",
                    "checkbox_not_found": "受取方法チェックボックスが見つかりません。",
                    "click_timeout": "画面要素のクリックがタイムアウトしました。",
                }
                err_msg = error_map.get(code, f"購入処理中に不明なエラーが発生しました（{code}）。")
        except Exception as exc:
            err_msg = str(exc)
        finally:
            self._go_start_url()
            self.signal_emitter.purchase_result_signal.emit(success, err_msg)

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd, payload = self.command_queue.get_nowait()
            except queue.Empty:
                break

            if cmd == "purchase":
                self._execute_purchase(payload or "")
            elif cmd == "go_start":
                self._go_start_url()
            else:
                self.signal_emitter.log_signal.emit(f"不明なコマンドを受信: {cmd}")

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
                self.signal_emitter.log_signal.emit(f"URL監視スクリプトの初期化に失敗: {exc}")

            if self.browser_context.pages:
                self.main_page = self.browser_context.pages[0]
            else:
                self.main_page = self.browser_context.new_page()

            self.main_page.set_default_timeout(60000)
            self._register_page(self.main_page)

            self.signal_emitter.log_signal.emit(f"開始URLを開きます: {START_URL}")
            self.main_page.goto(START_URL, wait_until="domcontentloaded")
            self._show_login_modal_if_needed()

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
                    self.signal_emitter.log_signal.emit("新しいタブを検出しました。")
                except Exception as exc:
                    self.signal_emitter.log_signal.emit(f"新規タブ監視の設定に失敗: {exc}")

            self.browser_context.on("page", on_new_page)
            self.signal_emitter.log_signal.emit("URL監視を開始しました。")

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

                self._drain_commands()
                time.sleep(0.25)

        except Exception as exc:
            self.signal_emitter.log_signal.emit(f"ブラウザ処理でエラーが発生: {exc}")
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
        self.add_log(f"対象URLを検知しました: {url}")

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Question)
        dialog.setWindowTitle("本当に購入しますか？")
        dialog.setText("本当に購入しますか？")
        yes_btn = dialog.addButton("はい", QMessageBox.YesRole)
        no_btn = dialog.addButton("いいえ", QMessageBox.NoRole)
        dialog.exec_()

        clicked = dialog.clickedButton()
        if clicked == yes_btn:
            self.add_log("「はい」が選択されました。購入処理を実行します。")
            self.command_queue.put(("purchase", url))
        else:
            self.add_log("「いいえ」が選択されました。開始URLへ戻ります。")
            self.command_queue.put(("go_start", None))

        self.confirm_pending = False

    def on_purchase_result(self, success: bool, error_message: str) -> None:
        if success:
            self.add_log("購入処理が完了しました。")
            QMessageBox.information(self, "購入結果", "購入処理が完了しました。開始URLに戻りました。")
            return

        msg = error_message or "不明なエラー"
        self.add_log(f"購入処理に失敗しました: {msg}")
        QMessageBox.warning(self, "購入結果", f"購入処理に失敗しました。\n{msg}\n開始URLに戻りました。")

    def on_browser_stopped(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText("状態: 停止中")
        self.add_log("ブラウザを停止しました。")

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
