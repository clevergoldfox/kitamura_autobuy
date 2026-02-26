# -*- coding: utf-8 -*-
"""
中古一覧クリック購入ツール
ユーザーが中古一覧ページで項目をクリックすると確認ダイアログを表示し、
「はい」で購入を実行する。
"""
import sys
import os
import json
import threading
import time
import queue
from PyQt5.QtCore import Qt, QPoint, QRectF, pyqtSignal, QObject, QTimer
from PyQt5.QtGui import QFont, QPainter, QBrush, QPen, QColor, QLinearGradient
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QHBoxLayout, QVBoxLayout, QFrame, QLineEdit,
    QTextEdit, QGroupBox
)

try:
    from playwright.sync_api import sync_playwright
    BROWSER_AVAILABLE = True
except Exception:
    BROWSER_AVAILABLE = False

# 中古一覧URL
LIST_URL = "https://shop.kitamura.jp/ec/list?type=u&limit=100&page=1&sort=update_date"


class SignalEmitter(QObject):
    log_signal = pyqtSignal(str)
    product_clicked_signal = pyqtSignal(str)   # クリックされた商品URL
    purchase_result_signal = pyqtSignal(bool, str)  # success, error_message


# 購入実行用JavaScript（既存ツールと同様）
PURCHASE_SCRIPT = """
async () => {
    const logs = [];
    const safeGet = (getter) => {
        if (typeof getter !== 'function') return null;
        try { return getter() || null; } catch (e) { return null; }
    };
    const waitForElement = (getter, timeout = 3000, interval = 150) => {
        return new Promise(resolve => {
            const deadline = Date.now() + timeout;
            let done = false;
            let pollId = null;
            let timerId = null;
            let observer = null;
            const finish = (el) => {
                if (done) return;
                done = true;
                if (pollId) clearInterval(pollId);
                if (timerId) clearTimeout(timerId);
                if (observer) observer.disconnect();
                resolve(el || null);
            };
            const tryGet = () => {
                const el = safeGet(getter);
                if (el) return finish(el);
                if (Date.now() >= deadline) return finish(null);
            };
            tryGet();
            pollId = setInterval(tryGet, interval);
            try {
                observer = new MutationObserver(tryGet);
                observer.observe(document.documentElement || document.body, { childList: true, subtree: true, attributes: true });
            } catch (e) {}
            timerId = setTimeout(() => finish(null), timeout);
        });
    };
    const findReceiveShopCheckbox = () => {
        const el = document.querySelector('#order-receive-shop');
        if (!el || el.type !== "checkbox" || el.offsetParent === null) return null;
        return el;
    };
    const fastClick = (xpaths, timeout = 3000) => {
        const targets = Array.isArray(xpaths) ? xpaths : [xpaths];
        return new Promise((resolve, reject) => {
            const start = Date.now();
            let clicked = false;
            const tryImmediate = () => {
                if (clicked) return true;
                for (const xpath of targets) {
                    const node = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                    if (node && node.offsetParent !== null) { clicked = true; node.click(); return true; }
                }
                return false;
            };
            if (tryImmediate()) { resolve(); return; }
            let obs = null, tid = null;
            const tryClick = () => {
                if (clicked) return true;
                for (const xpath of targets) {
                    const el = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                    if (el && el.offsetParent !== null) {
                        clicked = true;
                        if (obs) obs.disconnect();
                        if (tid) clearTimeout(tid);
                        el.click();
                        resolve();
                        return true;
                    }
                }
                return false;
            };
            obs = new MutationObserver(tryClick);
            obs.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style'] });
            const poll = () => {
                if (clicked) return;
                if (!tryClick() && Date.now() - start < timeout) setTimeout(poll, 0);
            };
            poll();
            tid = setTimeout(() => { if (obs) obs.disconnect(); if (!clicked) reject(new Error('Timeout')); }, timeout);
        });
    };
    if (document.querySelector('span.not-sales-text')) return { error: 'not_available' };
    try {
        await fastClick(["//button[contains(@class,'cart-button') and .//i[contains(@class,'cart-button-icon') and contains(@class,'fa-shopping-cart')]]"]);
        try {
            await fastClick([
                "//div[contains(@class,'cart-dialog') or contains(@class,'cart-dialog-basic')]//button[contains(@class,'cart-button') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'予約する')]]",
                "//div[contains(@class,'cart-dialog') or contains(@class,'cart-dialog-basic')]//button[contains(normalize-space(),'購入手続きへ進む')]",
            ], 800);
        } catch (e) {}
        await fastClick("//button[contains(@class,'action-btn') and contains(@class,'action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'購入手続きへ進む')]]");
        await new Promise(r => {
            if (window.location.href.includes('shop.kitamura.jp/ec/cart')) { r(); return; }
            const c = () => window.location.href.includes('shop.kitamura.jp/ec/cart') ? r() : setTimeout(c, 0);
            c();
        });
        await fastClick("//button[contains(@class,'v-btn--block') and contains(@class,'action-btn') and contains(@class,'action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'購入手続きへ進む')]]");
        await new Promise(r => {
            if (window.location.href.includes('shop.kitamura.jp/ec/order')) { r(); return; }
            const c = () => window.location.href.includes('shop.kitamura.jp/ec/order') ? r() : setTimeout(c, 0);
            c();
        });
        const checkbox = await waitForElement(findReceiveShopCheckbox, 3000, 50);
        if (!checkbox) return { error: 'checkbox_not_found', logs };
        await new Promise(res => setTimeout(res, 150));
        checkbox.click();
        if (!checkbox.checked) checkbox.click();
        await new Promise(res => setTimeout(res, 150));
        await fastClick("//button[contains(@class,'order-action-btn') and contains(@class,'order-action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'注文内容確認へ進む')]]");
        await new Promise(res => setTimeout(res, 100));
        await fastClick("//button[contains(@class,'error-dialog-btn') and .//span[contains(@class,'v-btn__content') and normalize-space()='OK']]");
        await fastClick("//button[contains(@class,'order-action-btn') and contains(@class,'order-action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'注文確定')]]");
        return { success: true, logs };
    } catch (e) {
        return { error: e.message, logs };
    }
}
"""


class PurchaseConfirmDialog(QFrame):
    """「本当に購入しますか？」はい/いいえ"""
    yes_clicked = pyqtSignal()
    no_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setFixedSize(360, 160)
        self.setStyleSheet("""
            PurchaseConfirmDialog {
                background-color: #f5f5f5;
                border: 2px solid #AA875F;
                border-radius: 8px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        msg = QLabel("本当に購入しますか？")
        msg.setStyleSheet("font-size: 16px; color: #333;")
        msg.setAlignment(Qt.AlignCenter)
        layout.addWidget(msg)
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        yes_btn = QPushButton("はい")
        yes_btn.setFixedHeight(40)
        yes_btn.setStyleSheet("""
            QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 5px; font-weight: bold; font-size: 14px; }
            QPushButton:hover { background-color: #45a049; }
        """)
        yes_btn.clicked.connect(self.yes_clicked.emit)
        no_btn = QPushButton("いいえ")
        no_btn.setFixedHeight(40)
        no_btn.setStyleSheet("""
            QPushButton { background-color: #9E9E9E; color: white; border: none; border-radius: 5px; font-weight: bold; font-size: 14px; }
            QPushButton:hover { background-color: #757575; }
        """)
        no_btn.clicked.connect(self.no_clicked.emit)
        btn_layout.addWidget(yes_btn)
        btn_layout.addWidget(no_btn)
        layout.addLayout(btn_layout)


class ResultDialog(QFrame):
    """購入結果表示 + 確認ボタン"""
    confirm_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(400, 200)
        self.setStyleSheet("""
            ResultDialog {
                background-color: #f5f5f5;
                border: 2px solid #AA875F;
                border-radius: 8px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        self.message_label = QLabel("")
        self.message_label.setStyleSheet("font-size: 14px; color: #333;")
        self.message_label.setWordWrap(True)
        self.message_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.message_label)
        self.confirm_btn = QPushButton("確認")
        self.confirm_btn.setFixedHeight(40)
        self.confirm_btn.setStyleSheet("""
            QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 5px; font-weight: bold; font-size: 14px; }
            QPushButton:hover { background-color: #45a049; }
        """)
        self.confirm_btn.clicked.connect(self.confirm_clicked.emit)
        layout.addWidget(self.confirm_btn)

    def set_message(self, success, error_message=""):
        if success:
            self.message_label.setText("正確に購入されました")
            self.message_label.setStyleSheet("font-size: 14px; color: #2E7D32;")
        else:
            self.message_label.setText("購入失敗\nエラー内容：" + (error_message or "不明"))
            self.message_label.setStyleSheet("font-size: 14px; color: #c62828;")


class CustomWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.header_height = 40
        self.border_thickness = 3
        self.drag_pos = None
        self.always_on_top = True

        self.data_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
        self.playwright = None
        self.shop_browser = None
        self.shop_page = None
        self.product_page = None  # 商品詳細を表示しているページ
        self.product_was_new_tab = False  # 商品が新しいタブで開かれたか
        self.browser_thread = None
        self.browser_running = False
        self.purchase_command_queue = queue.Queue()  # ('purchase', url) or ('close_product_tab',)
        self.notification_queue = queue.Queue()  # browser -> main: ('confirm', url)
        self.pending_click_url = None
        self.confirm_dialog = None
        self.result_dialog = None
        self._notification_timer = None

        self.signal_emitter = SignalEmitter()
        self.signal_emitter.log_signal.connect(self._add_log_safe)
        self.signal_emitter.product_clicked_signal.connect(
            self._on_product_clicked, Qt.QueuedConnection
        )
        self.signal_emitter.purchase_result_signal.connect(self._on_purchase_result)

        self.setWindowTitle("中古クリック購入ツール")
        self.resize(450, 520)
        self.init_ui()
        self.load_login_data()

    def init_ui(self):
        # Header
        self.title_bar = QFrame()
        self.title_bar.setFixedHeight(self.header_height)
        self.title_bar.setStyleSheet("background-color: transparent;")
        title_label = QLabel("中古クリック購入ツール")
        title_label.setStyleSheet("color: white; font-weight: bold; font-size: 17px;")
        title_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)

        self.btn_log_toggle = self._header_btn("📋", "#E8E0D5", "#AA875F")
        btn_pin = self._header_btn("📌", "#E8E0D5", "#AA875F")
        btn_min = self._header_btn("–", "#F5E6CA", "#AA875F")
        btn_close = self._header_btn("×", "#EAD7D1", "#AA4B3B")

        self.btn_log_toggle.clicked.connect(self.toggle_log_panel)
        btn_pin.clicked.connect(self.toggle_always_on_top)
        btn_min.clicked.connect(self.showMinimized)
        btn_close.clicked.connect(self.close)

        header_layout = QHBoxLayout(self.title_bar)
        header_layout.setContentsMargins(10, 0, 8, 0)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(self.btn_log_toggle)
        header_layout.addWidget(btn_pin)
        header_layout.addWidget(btn_min)
        header_layout.addWidget(btn_close)

        content = QFrame()
        content.setStyleSheet("background-color: #F9F9F9; border: none;")
        main_layout = QHBoxLayout(content)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Log panel (hidden by default)
        self.log_panel = QFrame()
        self.log_panel.setStyleSheet("background-color: #F9F9F9;")
        self.log_panel.setVisible(False)
        log_layout = QVBoxLayout(self.log_panel)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("background-color: #fff; border: 1px solid #ccc; font-size: 12px;")
        log_layout.addWidget(self.log_text)

        # Right panel
        self.right_panel = QFrame()
        self.right_panel.setStyleSheet("background-color: #F9F9F9; border: none;")
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setSpacing(10)

        # Login group
        login_group = QGroupBox("アカウント情報")
        login_group.setStyleSheet("""
            QGroupBox { font-weight: bold; font-size: 17px; color: #333; border: 2px solid #AA875F; border-radius: 5px; margin-top: 10px; padding-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
        """)
        login_group.setMaximumHeight(150)
        login_layout = QVBoxLayout()
        email_row = QHBoxLayout()
        email_row.addWidget(QLabel("メール:"))
        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("*****@gmail.com")
        email_row.addWidget(self.email_input, 1)
        password_row = QHBoxLayout()
        password_row.addWidget(QLabel("パスワード:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText("パスワード")
        password_row.addWidget(self.password_input, 1)
        btn_row = QHBoxLayout()
        self.save_login_btn = QPushButton("保存")
        self.save_login_btn.setFixedHeight(36)
        self.save_login_btn.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 3px; font-weight: bold; }")
        self.save_login_btn.clicked.connect(self.on_save_login)
        self.change_login_btn = QPushButton("変更")
        self.change_login_btn.setFixedHeight(36)
        self.change_login_btn.setStyleSheet("QPushButton { background-color: #FF9800; color: white; border: none; border-radius: 3px; font-weight: bold; }")
        self.change_login_btn.clicked.connect(self.on_save_login)
        self.clear_login_btn = QPushButton("クリア")
        self.clear_login_btn.setFixedHeight(36)
        self.clear_login_btn.setStyleSheet("QPushButton { background-color: #9E9E9E; color: white; border: none; border-radius: 3px; font-weight: bold; }")
        self.clear_login_btn.clicked.connect(self.on_clear_login)
        btn_row.addWidget(self.save_login_btn)
        btn_row.addWidget(self.change_login_btn)
        btn_row.addWidget(self.clear_login_btn)
        btn_row.addStretch()
        login_layout.addLayout(email_row)
        login_layout.addLayout(password_row)
        login_layout.addLayout(btn_row)
        login_group.setLayout(login_layout)
        right_layout.addWidget(login_group)

        # Start button
        self.start_btn = QPushButton("スタート")
        self.start_btn.setFixedHeight(50)
        self.start_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #42A5F5, stop:1 #1E88E5);
                color: #fff; border-radius: 8px; font-weight: bold; font-size: 18px;
            }
            QPushButton:hover { background: #1E88E5; }
            QPushButton:disabled { background: #BDBDBD; color: #757575; }
        """)
        self.start_btn.clicked.connect(self.on_start)
        right_layout.addWidget(self.start_btn)
        right_layout.addStretch()

        main_layout.addWidget(self.log_panel)
        main_layout.addWidget(self.right_panel, 1)

        # Main vertical
        main_v = QVBoxLayout(self)
        main_v.setContentsMargins(0, 0, 0, 0)
        main_v.setSpacing(0)
        main_v.addWidget(self.title_bar)
        main_v.addWidget(content, 1)

    def _header_btn(self, text, bg, border):
        btn = QPushButton(text)
        btn.setFixedSize(36, 36)
        btn.setStyleSheet(f"""
            QPushButton {{ background-color: {bg}; color: #333; border: 2px solid {border}; border-radius: 15px; font-size: 16px; }}
            QPushButton:hover {{ background-color: #fac069; border: 2px solid white; }}
        """)
        return btn

    def _add_log_safe(self, msg):
        self.log_text.append(msg)
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def add_log(self, msg):
        self.signal_emitter.log_signal.emit(msg)

    def toggle_log_panel(self):
        if self.log_panel.isVisible():
            self.log_panel.setVisible(False)
            self.right_panel.setVisible(True)
        else:
            self.log_panel.setVisible(True)
            self.right_panel.setVisible(False)

    def toggle_always_on_top(self):
        self.always_on_top = not self.always_on_top
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.always_on_top)
        self.show()

    def load_login_data(self):
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.email_input.setText(data.get("email", ""))
                    self.password_input.setText(data.get("password", ""))
        except Exception:
            pass

    def on_save_login(self):
        data = {"email": self.email_input.text(), "password": self.password_input.text()}
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.add_log("✓ ログイン情報を保存しました")
        except Exception as e:
            self.add_log(f"保存エラー: {e}")

    def on_clear_login(self):
        self.email_input.clear()
        self.password_input.clear()
        self.add_log("ログイン情報をクリアしました")

    def _on_product_clicked(self, url):
        try:
            self.pending_click_url = url
            self.raise_()
            self.activateWindow()
            if not self.confirm_dialog:
                self.confirm_dialog = PurchaseConfirmDialog(self)
                self.confirm_dialog.yes_clicked.connect(self._on_confirm_yes)
                self.confirm_dialog.no_clicked.connect(self._on_confirm_no)
            self.confirm_dialog.setWindowTitle("購入確認")
            self.confirm_dialog.show()
            self.confirm_dialog.raise_()
            self.confirm_dialog.activateWindow()
            self.confirm_dialog.setWindowState((self.confirm_dialog.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
            QApplication.instance().processEvents()
            self.add_log("購入確認を表示しました")
        except Exception as e:
            self.add_log(f"❌ 購入確認の表示に失敗しました: {e}")

    def _on_confirm_yes(self):
        if self.confirm_dialog:
            self.confirm_dialog.hide()
        url = self.pending_click_url
        self.pending_click_url = None
        if url:
            self.purchase_command_queue.put(("purchase", url))
            self.add_log(f"購入を開始します: {url}")

    def _on_confirm_no(self):
        if self.confirm_dialog:
            self.confirm_dialog.hide()
        self.pending_click_url = None
        self.purchase_command_queue.put(("close_product_tab", None))
        self.add_log("購入をキャンセルしました")

    def _on_purchase_result(self, success, error_message):
        if not self.result_dialog:
            self.result_dialog = ResultDialog(self)
            self.result_dialog.confirm_clicked.connect(self._on_result_confirm)
        self.result_dialog.set_message(success, error_message)
        self.result_dialog.show()
        self.result_dialog.raise_()
        self.result_dialog.activateWindow()
        if success:
            QTimer.singleShot(5000, self._on_result_confirm)

    def _on_result_confirm(self):
        if self.result_dialog:
            self.result_dialog.hide()

    def _poll_notification_queue(self):
        """Main-thread timer: show purchase confirm when browser thread puts one."""
        if not self.browser_running:
            if self._notification_timer:
                self._notification_timer.stop()
                self._notification_timer = None
            return
        try:
            while True:
                item = self.notification_queue.get_nowait()
                if item[0] == "confirm" and item[1]:
                    self._on_product_clicked(item[1])
        except queue.Empty:
            pass

    def on_start(self):
        if not BROWSER_AVAILABLE:
            self.add_log("❌ Playwrightが利用できません。pip install playwright を実行してください")
            return
        if self.browser_running:
            self.add_log("⚠ ブラウザは既に起動中です")
            return
        self.browser_running = True
        if not self._notification_timer:
            self._notification_timer = QTimer(self)
            self._notification_timer.timeout.connect(self._poll_notification_queue)
        self._notification_timer.start(250)
        self.browser_thread = threading.Thread(target=self._browser_loop, daemon=True)
        self.browser_thread.start()
        self.add_log("🌐 ブラウザを起動しています...")

    def _browser_loop(self):
        try:
            profile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profile")
            self.playwright = sync_playwright().start()
            launch_kwargs = {
                "user_data_dir": profile_path,
                "headless": False,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            try:
                self.shop_browser = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
                self.add_log("✓ Playwright Chromiumを使用します")
            except Exception:
                try:
                    self.shop_browser = self.playwright.chromium.launch_persistent_context(channel="msedge", **launch_kwargs)
                    self.add_log("✓ Microsoft Edgeを使用します")
                except Exception:
                    self.shop_browser = self.playwright.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
                    self.add_log("✓ Google Chromeを使用します")

            if len(self.shop_browser.pages) > 0:
                self.shop_page = self.shop_browser.pages[0]
            else:
                self.shop_page = self.shop_browser.new_page()
            self.shop_page.set_default_timeout(60000)
            self.product_page = None

            def on_new_page(page):
                self.product_page = page
                try:
                    page.set_default_timeout(60000)
                except Exception:
                    pass

            self.shop_browser.on("page", on_new_page)
            self.add_log("✓ ブラウザを起動しました")

            # 中古一覧へ
            self.add_log("📄 中古一覧ページを開いています...")
            self.shop_page.goto(LIST_URL, wait_until="domcontentloaded")
            time.sleep(1.2)

            # 未ログインならログインモーダルを表示する（ヘッダーの「ログイン」をクリック）
            try:
                html = self.shop_page.content()
                # ログイン済みの目安: 会員情報・マイページ等がある / ログインモーダル用の文言がない
                has_login_modal = "あなたのアカウントにログイン" in html or ("ログイン" in html and "パスワード" in html and "新規会員登録" in html)
                looks_logged_in = "会員情報" in html or "マイページ" in html or "ログアウト" in html

                if not looks_logged_in and not has_login_modal:
                    self.add_log("🔐 未ログインのため、ログインモーダルを表示します...")
                    # ヘッダーの「ログイン」リンク/ボタンをクリックしてモーダルを開く
                    clicked = self.shop_page.evaluate("""
                        () => {
                            const links = document.querySelectorAll('a, button, span[role="button"]');
                            for (const el of links) {
                                const text = (el.textContent || '').trim();
                                if (text === 'ログイン' || text === 'ログインする') {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                    if clicked:
                        time.sleep(0.6)
                        self.add_log("✓ ログインモーダルを表示しました。メールとパスワードでログインしてください。")
                    else:
                        self.add_log("⚠ ログインボタンが見つかりませんでした。手動でログインしてください。")
                elif has_login_modal:
                    self.add_log("✓ ログインモーダルが表示されています。メールとパスワードでログインしてください。")
                else:
                    self.add_log("✓ ログイン済みのようです。")
            except Exception as e:
                self.add_log(f"⚠ ログイン状態の確認中にエラー: {e}")

            # ログインモーダルが開いている間はユーザーがログインするまで待つ（モーダルが消える or 一覧が使える状態になるまで）
            for _ in range(120):
                try:
                    html = self.shop_page.content()
                    # モーダルがまだある = まだログインしていない可能性
                    if "あなたのアカウントにログイン" in html:
                        time.sleep(1)
                        continue
                    # 会員情報などがあればログイン済み
                    if "会員情報" in html or "ログアウト" in html or "マイページ" in html:
                        break
                    # 一覧ページにいるなら続行
                    if "list?type=u" in self.shop_page.url:
                        break
                except Exception:
                    pass
                time.sleep(1)
            else:
                pass

            self.add_log("✓ 中古一覧を表示しました。商品ページを開くと購入確認が出ます。")

            self._product_dialog_pending = False  # ダイアログ表示中は再検出しない
            self._last_debug_logged_url = None  # デバッグ重複防止
            self._poll_count = 0

            def on_product_page_opened(page, url, is_new_tab):
                self.product_page = page
                self.product_was_new_tab = is_new_tab
                self._product_dialog_pending = True
                self.add_log("🔔 商品ページを検出しました。購入確認を表示します。")
                self.signal_emitter.product_clicked_signal.emit(url)
                try:
                    self.notification_queue.put_nowait(("confirm", url))
                except Exception:
                    pass

            def _is_product_url(url):
                if not url or "kitamura" not in url:
                    return False
                if "/ec/used/" not in url or "/ec/list" in url:
                    return False
                return True

            def check_all_pages_for_product_url():
                if self._product_dialog_pending or not self.shop_browser:
                    return
                self._poll_count += 1
                try:
                    pages = self.shop_browser.pages()
                    # 約10秒ごとに現在のタブ数とURLをログ（原因切り分け用）
                    if self._poll_count % 50 == 1 and self._poll_count > 1:
                        try:
                            infos = []
                            for i, p in enumerate(pages):
                                if p.is_closed():
                                    infos.append(f"タブ{i+1}: (閉じた)")
                                else:
                                    u = (p.url or "")[:70]
                                    infos.append(f"タブ{i+1}: {u}")
                            self.add_log("🔍 現在のタブ: " + " | ".join(infos))
                        except Exception:
                            pass
                    for p in pages:
                        if p.is_closed():
                            continue
                        try:
                            # 常に location.href を優先（同一タブ遷移で p.url が遅れる場合がある）
                            url = p.url or ""
                            try:
                                href = p.evaluate("() => typeof window !== 'undefined' && window.location && window.location.href ? window.location.href : ''")
                                if href:
                                    url = href
                            except Exception:
                                pass
                            if not _is_product_url(url):
                                if "ec/used" in url:
                                    u = (url or "")[:85]
                                    if u != getattr(self, "_last_debug_logged_url", None):
                                        self._last_debug_logged_url = u
                                        self.add_log("🔍 ec/used のURLを検出しましたがスキップ: " + u)
                                continue
                            if url != getattr(self, "_last_debug_logged_url", None):
                                self._last_debug_logged_url = url
                                self.add_log("🔍 商品URLを検出しました: " + (url[:80] or "") + " -> 購入確認を表示")
                            on_product_page_opened(p, url, p != self.shop_page)
                            return
                        except Exception:
                            continue
                except Exception:
                    pass

            # 一覧タブが商品ページへ遷移したとき（即時検出）
            def on_list_framenavigated(frame):
                try:
                    if frame != self.shop_page.main_frame:
                        return
                    url = frame.url or ""
                    if not _is_product_url(url):
                        try:
                            url = self.shop_page.url or url
                            if not _is_product_url(url):
                                url = self.shop_page.evaluate("() => window.location.href || ''") or url
                        except Exception:
                            pass
                    if not _is_product_url(url):
                        _inject_url_watcher(self.shop_page)
                        return
                    if not self._product_dialog_pending:
                        on_product_page_opened(self.shop_page, url, False)
                    _inject_url_watcher(self.shop_page)
                except Exception:
                    pass

            self.shop_page.on("framenavigated", on_list_framenavigated)

            # 新しいタブで商品ページが開いたとき（即時検出）
            def on_new_page(page):
                try:
                    page.set_default_timeout(60000)

                    def on_new_page_framenavigated(frame):
                        try:
                            if frame != page.main_frame:
                                return
                            url = frame.url
                            if not _is_product_url(url):
                                return
                            if not self._product_dialog_pending:
                                on_product_page_opened(page, url, True)
                        except Exception:
                            pass

                    page.on("framenavigated", on_new_page_framenavigated)
                    _inject_url_watcher(page)
                except Exception:
                    pass

            self.shop_browser.on("page", on_new_page)

            # ページ内でURLが変わったら即通知する（SPA・pushState 対応）
            URL_WATCHER_SCRIPT = """
                (function() {
                    if (window.__urlWatcherStarted) return;
                    window.__urlWatcherStarted = true;
                    window.__lastUrl = location.href;
                    setInterval(function() {
                        var h = location.href;
                        if (window.__lastUrl !== h) {
                            window.__lastUrl = h;
                            if (window.notifyUrlChange) window.notifyUrlChange(h);
                        }
                    }, 150);
                })();
            """

            _url_binding_pages = set()  #  binding を登録済みのページ

            def _inject_url_watcher(p):
                try:
                    if p.is_closed():
                        return
                    # 各ページで binding を有効に（同一タブ遷移後も確実に通知）
                    try:
                        pid = id(p)
                        if pid not in _url_binding_pages:
                            p.expose_binding("notifyUrlChange", _on_url_changed)
                            _url_binding_pages.add(pid)
                    except Exception:
                        pass
                    p.evaluate(URL_WATCHER_SCRIPT)
                except Exception:
                    pass

            def _on_url_changed(source, href):
                try:
                    if not href or not _is_product_url(href):
                        return
                    if self._product_dialog_pending:
                        return
                    page = getattr(source, "page", None)
                    if not page or page.is_closed():
                        return
                    on_product_page_opened(page, href, page != self.shop_page)
                except Exception:
                    pass

            try:
                self.shop_browser.add_init_script(URL_WATCHER_SCRIPT)
                _inject_url_watcher(self.shop_page)
            except Exception as e:
                self.add_log("⚠ URL変更のリアルタイム検出を有効にできませんでした: " + str(e))

            # リアルタイム検出: 全タブのURLを定期的にチェック（フォールバック）
            self.add_log("✓ 商品ページをリアルタイム検出（URL変更を即時検知・購入確認を表示）")

            # コマンドループ + リアルタイムURLチェック
            while self.browser_running and self.shop_page:
                check_all_pages_for_product_url()
                try:
                    cmd = self.purchase_command_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                self._product_dialog_pending = False
                if cmd[0] == "close_product_tab":
                    try:
                        if self.product_page and not self.product_page.is_closed():
                            if self.product_was_new_tab:
                                self.product_page.close()
                            else:
                                self.product_page.goto(LIST_URL, wait_until="domcontentloaded")
                        self.product_page = None
                        self.add_log("📄 商品ページを閉じました")
                    except Exception as e:
                        self.add_log(f"閉じる際のエラー: {e}")
                        self.product_page = None
                    continue
                if cmd[0] == "go_back":
                    try:
                        self.shop_page.goto(LIST_URL, wait_until="domcontentloaded")
                        self.add_log("📄 中古一覧に戻りました")
                    except Exception as e:
                        self.add_log(f"一覧に戻る際のエラー: {e}")
                    continue
                if cmd[0] == "purchase":
                    _, product_url = cmd
                    success = False
                    err_msg = ""
                    page_to_use = self.product_page if self.product_page and not self.product_page.is_closed() else None
                    if not page_to_use:
                        self.add_log("⚠ 商品ページが見つかりません")
                        self.signal_emitter.purchase_result_signal.emit(False, "商品ページが閉じられている可能性があります")
                        continue
                    try:
                        result = page_to_use.evaluate(PURCHASE_SCRIPT)
                        if result.get("error"):
                            err_map = {
                                "not_available": "商品が販売可能ではありません",
                                "Timeout": "タイムアウト",
                                "checkbox_not_found": "受取店舗のチェックボックスが見つかりません",
                            }
                            err_msg = err_map.get(result["error"], result["error"])
                        else:
                            success = bool(result.get("success"))
                    except Exception as e:
                        err_msg = str(e)
                    try:
                        if self.product_page and not self.product_page.is_closed():
                            if self.product_was_new_tab:
                                self.product_page.close()
                            else:
                                self.product_page.goto(LIST_URL, wait_until="domcontentloaded")
                        self.product_page = None
                    except Exception:
                        pass
                    self.signal_emitter.purchase_result_signal.emit(success, err_msg)
                self._product_dialog_pending = False

        except Exception as e:
            self.add_log(f"❌ ブラウザエラー: {str(e)}")
            import traceback
            self.add_log(traceback.format_exc())
        finally:
            self.browser_running = False
            try:
                if self.playwright:
                    self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
            self.shop_browser = None
            self.shop_page = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 枠
        grad = QLinearGradient(0, 0, self.width(), self.height())
        c1, c2 = QColor("#AA875F"), QColor("#6B7A76")
        c1.setAlphaF(0.5)
        c2.setAlphaF(0.5)
        grad.setColorAt(0, c1)
        grad.setColorAt(1, c2)
        painter.setPen(QPen(QBrush(grad), self.border_thickness))
        painter.setBrush(Qt.NoBrush)
        rect = QRectF(
            self.border_thickness / 2, self.border_thickness / 2,
            self.width() - self.border_thickness, self.height() - self.border_thickness
        )
        painter.drawRoundedRect(rect, 8, 8)
        # ヘッダー背景
        hgrad = QLinearGradient(0, 0, self.width(), 0)
        h1, h2 = QColor("#AA875F"), QColor("#6B7A76")
        h1.setAlphaF(0.9)
        h2.setAlphaF(0.9)
        hgrad.setColorAt(0, h1)
        hgrad.setColorAt(1, h2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(hgrad)
        painter.drawRoundedRect(
            self.border_thickness, self.border_thickness,
            self.width() - self.border_thickness * 2, self.header_height,
            6, 6
        )
        painter.drawRect(
            self.border_thickness, self.border_thickness + self.header_height - 4,
            self.width() - self.border_thickness * 2, 6
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.y() < self.header_height:
            self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self.drag_pos is not None:
            self.move(event.globalPos() - self.drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        self.browser_running = False
        event.accept()


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 9))
    w = CustomWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
