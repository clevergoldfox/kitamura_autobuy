import sys
import re
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta
import queue
from PyQt5.QtCore import Qt, QPoint, QRectF, QByteArray, pyqtSignal, QObject
from PyQt5.QtGui import (
    QPainter, QBrush, QPen, QColor, QLinearGradient, QFont, QIcon, QPixmap
)
from PyQt5.QtWidgets import QGraphicsBlurEffect
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QHBoxLayout, QVBoxLayout, QFrame, QLineEdit,
    QTableWidget, QTableWidgetItem, QTextEdit,
    QHeaderView, QScrollArea, QGroupBox
)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
    import asyncio
    BROWSER_AVAILABLE = True
except Exception as e:
    # Broadly catch any import-time failure (including environment conflicts that remove traceback.extract_stack)
    BROWSER_AVAILABLE = False
    print(f"Warning: playwright unavailable ({type(e).__name__}: {e}). Install with: pip install playwright")


class SignalEmitter(QObject):
    """Thread-safe signal emitter for UI updates"""
    log_signal = pyqtSignal(str)
    update_last_log_signal = pyqtSignal(str)  # Signal to update last log line
    button_state_signal = pyqtSignal(bool)  # True = monitoring active, False = stopped
    manual_auth_done_signal = pyqtSignal()  # Signal when manual auth is complete
    show_auth_dialog_signal = pyqtSignal()  # Signal to show auth confirmation dialog
    update_timer_signal = pyqtSignal(str)  # Signal to update speed test timer


class AuthConfirmDialog(QFrame):
    """Overlay dialog for confirming manual purchase completion - appears within main window"""
    confirmed = pyqtSignal()
    cancelled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(350, 180)

        # Dialog container with border
        self.setStyleSheet("""
            AuthConfirmDialog {
                background-color: #f5f5f5;
                border: 2px solid #AA875F;
                border-radius: 8px;
            }
        """)

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # Message label
        message = QLabel("ブラウザで手動購入を行ってください。\n電話番号認証が完了したら\n「確認」ボタンを押してください。")
        message.setStyleSheet("""
            QLabel {
                font-size: 14px;
                color: #333;
                line-height: 1.5;
                background: transparent;
                border: none;
            }
        """)
        message.setWordWrap(True)
        message.setAlignment(Qt.AlignCenter)
        layout.addWidget(message)

        # Button layout
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        
        # Confirm button
        confirm_btn = QPushButton("確認")
        confirm_btn.setFixedHeight(40)
        confirm_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 20px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)
        confirm_btn.clicked.connect(self.on_confirm)

        # btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        layout.addLayout(btn_layout)

    def on_confirm(self):
        self.confirmed.emit()

    def on_cancel(self):
        self.cancelled.emit()


class CustomWindow(QWidget):
    


    def _single_browser_monitoring_loop(self, browser_obj, all_processed_urls):
        """Monitor a single browser/page continuously in its own thread - real-time detection with continuous reload"""
        try:
            page = browser_obj['page']
            url = browser_obj['url']
            name = browser_obj['name']
            product_filter = browser_obj['product_filter']
            
            # Create event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            PAGE_REFRESH_INTERVAL = 1.0  # seconds between reloads (real-time monitoring)
            refresh_count = 0
            first_load = True
            
            self.add_log(f"🔍 {name}のリアルタイム監視を開始: {url}")
            
            while self.monitoring_active:
                try:
                    refresh_count += 1
                    
                    # First load: navigate to page, subsequent: reload for faster performance
                    if first_load:
                        loop.run_until_complete(
                            page.goto(url, wait_until="domcontentloaded")
                        )
                        first_load = False
                    else:
                        # Use reload() for faster real-time detection
                        loop.run_until_complete(
                            page.reload(wait_until="domcontentloaded")
                        )
                    
                    self.update_last_log(f"🔍 {name}リアルタイム検知中... ({refresh_count}回目)")
                    
                    # Extract product detail links in real-time
                    product_detail_links = self.extract_product_links_by_filter_fast(
                        page,
                        product_filter,
                        loop=loop
                    )
                    
                    # Process detected links immediately
                    if product_detail_links:
                        with self.processed_urls_lock:
                            new_products = [link for link in product_detail_links if link not in all_processed_urls]
                            
                            if new_products:
                                self.add_log(f"🎯 {name}: {len(new_products)}件の対象商品をリアルタイム検知 → 購入キューに追加")
                                for detail_link in new_products:
                                    self.purchase_queue.put(detail_link)
                                    all_processed_urls.add(detail_link)
                                    
                                    # Clean up old URLs if set gets too large
                                    if len(all_processed_urls) > 500:
                                        urls_to_remove = list(all_processed_urls)[:100]
                                        for url_to_remove in urls_to_remove:
                                            all_processed_urls.discard(url_to_remove)
                    
                    # Continue monitoring - reload again after interval (real-time loop)
                    time.sleep(PAGE_REFRESH_INTERVAL)
                    
                except Exception as e:
                    self.add_log(f"{name}リアルタイム検知エラー: {str(e)}")
                    # On error, try to reload the page on next iteration
                    first_load = True
                    time.sleep(1)
            
            self.add_log(f"終了: {name}のリアルタイム監視を停止しました")
            loop.close()
            
        except Exception as e:
            self.add_log(f"{name}リアルタイム監視中エラー: {str(e)}")
            import traceback
            self.add_log(f"詳細: {traceback.format_exc()}")

    def _detection_monitoring_loop(self, products):
        """Detection browser loop: launches multiple browsers in parallel, each monitoring one page"""
        try:
            if not self.detection_browsers:
                self.add_log("エラー: 検知ブラウザを利用できません")
                return

            if not products:
                self.add_log("警告: 監視する商品リストが空です")
                return

            all_processed_urls = set()
            MAX_PROCESSED_URLS = 500

            self.add_log(f"開始: リアルタイム検知モード（{len(self.detection_browsers)}個のブラウザで並列リアルタイム監視）")
            self.add_log(f"各ブラウザは1秒間隔で自動リロードし、商品リンクをリアルタイム検出します")

            # Start a monitoring thread for each browser (all run in parallel)
            monitoring_threads = []
            for browser_obj in self.detection_browsers:
                thread = threading.Thread(
                    target=self._single_browser_monitoring_loop,
                    args=(browser_obj, all_processed_urls),
                    daemon=True
                )
                thread.start()
                monitoring_threads.append(thread)
                self.add_log(f"✓ {browser_obj['name']}のリアルタイム監視スレッドを開始しました（URL: {browser_obj['url']}）")

            # Wait for all threads (they will run until monitoring_active is False)
            for thread in monitoring_threads:
                thread.join()

            self.add_log("終了: 検知監視を停止しました")

        except Exception as e:
            self.add_log(f"検知監視中エラー: {str(e)}")
            import traceback
            self.add_log(f"詳細: {traceback.format_exc()}")

    def _purchase_processing_loop(self):
        """Purchase browser loop: reads URLs from queue and makes purchases"""
        try:
            if self.shop_page is None:
                self.add_log("エラー: 購入ブラウザを利用できません")
                return

            self.add_log("💰 購入処理を開始します")

            purchase_count = 0
            while self.monitoring_active:
                try:
                    # Wait for URLs from detection browser (with timeout to check monitoring_active)
                    try:
                        product_url = self.purchase_queue.get(timeout=1.0)
                        purchase_count += 1
                        self.add_log(f"💰 購入開始 {purchase_count}: {product_url}")
                        
                        success = self.process_product_detail_page(
                            self.shop_page,
                            product_url,
                            purchase_count,
                            1
                        )
                        
                        if success:
                            self.add_log(f"✅ 購入成功 ({purchase_count}件目)")
                        else:
                            self.add_log(f"❌ 購入失敗 ({purchase_count}件目)")
                    
                    except queue.Empty:
                        # No products in queue, continue waiting
                        continue

                except Exception as e:
                    self.add_log(f"購入処理エラー: {str(e)}")
                    time.sleep(0.5)

            self.add_log(f"終了: 購入処理を停止しました（合計{purchase_count}件処理）")

        except Exception as e:
            self.add_log(f"購入処理中エラー: {str(e)}")
            import traceback
            self.add_log(f"詳細: {traceback.format_exc()}")

    def _continue_monitoring_with_browser(self, products):
        """Start both detection and purchase threads"""
        try:
            # Start detection thread (monitors and finds products)
            self.add_log("🚀 検知スレッドを開始します")
            self.detection_thread = threading.Thread(target=self._detection_monitoring_loop, args=(products,))
            self.detection_thread.daemon = True
            self.detection_thread.start()

            # Start purchase thread (purchases from queue)
            self.add_log("🚀 購入スレッドを開始します")
            self.purchase_thread = threading.Thread(target=self._purchase_processing_loop)
            self.purchase_thread.daemon = True
            self.purchase_thread.start()

            # Wait for both threads to finish (they'll stop when monitoring_active is False)
            self.detection_thread.join()
            self.purchase_thread.join()

        except Exception as e:
            self.add_log(f"監視開始エラー: {str(e)}")
            import traceback
            self.add_log(f"詳細: {traceback.format_exc()}")


    # === Button Event Handlers ===
    def on_search(self):
        """Handle save button click - add or update product in table"""
        # Get input values
        product_name = self.search_input.text().strip()
        min_price_text = self.min_price_input.text().strip()
        max_price_text = self.max_price_input.text().strip()

        # Validate product name
        if not product_name:
            self.add_log("⚠ 商品名を入力してください")
            return

        # Parse prices
        min_price = None
        max_price = None

        if min_price_text:
            try:
                min_price = int(min_price_text)
                if min_price < 0:
                    self.add_log("⚠ 下限価格は0以上の値を入力してください")
                    return
            except ValueError:
                self.add_log("⚠ 下限価格は数値で入力してください")
                return

        if max_price_text:
            try:
                max_price = int(max_price_text)
                if max_price < 0:
                    self.add_log("⚠ 上限価格は0以上の値を入力してください")
                    return
            except ValueError:
                self.add_log("⚠ 上限価格は数値で入力してください")
                return

        # Validate min <= max
        if min_price is not None and max_price is not None:
            if min_price > max_price:
                self.add_log("⚠ 下限価格は上限価格以下にしてください")
                return

        # No URL needed - all products monitored on single page
        # Fixed monitoring URL: https://shop.kitamura.jp/ec/list?type=u&sort=update_date&limit=40

        # Check if we're editing or adding
        if self.editing_row is not None:
            # Update existing row
            self.update_product_row(self.editing_row, product_name, min_price, max_price)

            # Log success
            price_info = ""
            if min_price is not None and max_price is not None:
                price_info = f" ({min_price:,}¥ ~ {max_price:,}¥)"
            elif min_price is not None:
                price_info = f" ({min_price:,}¥以上)"
            elif max_price is not None:
                price_info = f" ({max_price:,}¥以下)"

            self.add_log(f"✓ 商品を更新しました: {product_name}{price_info}")
        else:
            # Add new product to table
            self.add_product_to_table(product_name, min_price, max_price)

            # Log success
            price_info = ""
            if min_price is not None and max_price is not None:
                price_info = f" ({min_price:,}¥ ~ {max_price:,}¥)"
            elif min_price is not None:
                price_info = f" ({min_price:,}¥以上)"
            elif max_price is not None:
                price_info = f" ({max_price:,}¥以下)"

            self.add_log(f"✓ 商品を追加しました: {product_name}{price_info}")

        # Save to JSON
        self.save_products_to_json()

        # Clear input fields and reset editing state
        self.search_input.clear()
        self.min_price_input.clear()
        self.max_price_input.clear()
        self.search_input.setFocus()
        self.editing_row = None


    def _run_single_speed_test_with_page(self, page, test_url, test_num):
        """ULTRA-FAST purchase execution - no timing, maximum speed"""
        try:
            # Navigate to page instantly
            page.goto(test_url, wait_until="commit")

            # Execute entire purchase flow in ONE JavaScript call for maximum speed
            result = page.evaluate("""
                async () => {
                const logs = [];

                // Safe getter wrapper to avoid runtime errors
                const safeGet = (getter) => {
                        if (typeof getter !== 'function') return null;
                        try { return getter() || null; } catch (e) { return null; }
                };
                // Wait for element once (poll + MutationObserver); resolves a single time
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

                            // Immediate attempt
                            tryGet();

                            // Polling
                            pollId = setInterval(tryGet, interval);

                            // DOM observer for fast detection
                            try {
                                observer = new MutationObserver(tryGet);
                                observer.observe(document.documentElement || document.body, { childList: true, subtree: true, attributes: true });
                            } catch (e) {}

                            // Hard timeout
                            timerId = setTimeout(() => finish(null), timeout);
                        });
                };

                // Locate the receive shop checkbox by id (no error)
                const findReceiveShopCheckbox = () => {
                        const el = document.querySelector('#order-receive-shop');
                        if (!el) return null;
                        if (el.type !== "checkbox") return null;
                        if (el.offsetParent === null) return null; // hidden
                        return el;
                };
                
                // ULTRA-FAST click function with MutationObserver (supports multiple targets)
                const fastClick = (xpaths, timeout = 3000) => {
                        const targets = Array.isArray(xpaths) ? xpaths : [xpaths];
                        return new Promise((resolve, reject) => {
                            const start = Date.now();
                            let clicked = false;

                            // Immediate check
                            const tryImmediate = () => {
                                if (clicked) return true;
                                for (const xpath of targets) {
                                    const node = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                                    if (node && node.offsetParent !== null) {
                                            clicked = true;
                                            node.click();
                                            return true;
                                    }
                                }
                                return false;
                            };

                            if (tryImmediate()) {
                                resolve();
                                return;
                            }

                            let obs = null;
                            let tid = null;

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

                            // MutationObserver for instant detection
                            obs = new MutationObserver(tryClick);
                            obs.observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style']});

                            // 0ms polling fallback
                            const poll = () => {
                                if (clicked) return;
                                if (!tryClick() && Date.now() - start < timeout) {
                                    setTimeout(poll, 0);
                                }
                            };
                            poll();

                            tid = setTimeout(() => { if (obs) obs.disconnect(); if (!clicked) reject(new Error('Timeout')); }, timeout);
                        });
                };



                // Simple logger for debugging inside evaluate
                const print = (msg) => { logs.push(String(msg)); console.log(msg); };

                // Immediately dismiss modal dialogs with an OK button
                const clickOkButtons = () => {
                        const candidates = Array.from(document.querySelectorAll('button, a, span, div'));
                        for (const el of candidates) {
                            const text = (el.textContent || '').trim();
                            if (/^OK$/i.test(text)) {
                                try { el.click(); return true; } catch (e) {}
                            }
                        }
                        const byRole = document.querySelector('[role=\"button\"][title=\"OK\"], [role=\"button\"][aria-label=\"OK\"]');
                        if (byRole) { try { byRole.click(); return true; } catch (e) {} }
                        return false;
                };

                // Check availability
                if (document.querySelector('span.not-sales-text')) return { error: 'not_available' };

                // Execute all steps
                try {
                        await fastClick([
                            "//button[contains(@class,'cart-button') and .//i[contains(@class,'cart-button-icon') and contains(@class,'fa-shopping-cart')]]",
                        ]);
                        // Confirm in modal dialog if it appears
                        try {
                            await fastClick([
                                "//div[contains(@class,'cart-dialog') or contains(@class,'cart-dialog-basic')]//button[contains(@class,'cart-button') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'予約する')]]",
                                "//div[contains(@class,'cart-dialog') or contains(@class,'cart-dialog-basic')]//button[contains(normalize-space(),'購入手続きへ進む')]",
                            ], 800);
                        } catch (e) {
                            // Modal may not appear; fall through to direct proceed button
                        }
                        await fastClick("//button[contains(@class,'action-btn') and contains(@class,'action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'購入手続きへ進む')]]");

                        // Wait for cart page
                        await new Promise(r => {
                            if (window.location.href.includes('shop.kitamura.jp/ec/cart')) { r(); return; }
                            const c = () => window.location.href.includes('shop.kitamura.jp/ec/cart') ? r() : setTimeout(c, 0);
                            c();
                        });

                        await fastClick("//button[contains(@class,'v-btn--block') and contains(@class,'action-btn') and contains(@class,'action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'購入手続きへ進む')]]");

                        // Wait for order page
                        await new Promise(r => {
                            if (window.location.href.includes('shop.kitamura.jp/ec/order')) { r(); return;}
                            const c = () => window.location.href.includes('shop.kitamura.jp/ec/order') ? r() : setTimeout(c, 0);
                            c();
                        });
                        
                        // Attempt to click checkbox for "受取店舗" ONLY on /order page
                        const checkbox = await waitForElement(findReceiveShopCheckbox, 3000, 50);
                        if (!checkbox) {
                            print('checkbox not found (#order-receive-shop)');
                            return { error: 'checkbox_not_found', logs };
                        }
                        
                        await new Promise(res => setTimeout(res, 150));
                        checkbox.click();
                        print('----------(1step)-------------');

                        if (!checkbox.checked) {
                            checkbox.click();
                        }
                        print('----------(2step)-------------');
                        await new Promise(res => setTimeout(res, 150));
                        await fastClick("//button[contains(@class,'order-action-btn') and contains(@class,'order-action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'注文内容確認へ進む')]]");
                        await new Promise(res => setTimeout(res, 100));
                            
                        print('----------(3step)-------------');
                        await fastClick("//button[contains(@class,'error-dialog-btn') and .//span[contains(@class,'v-btn__content') and normalize-space()='OK']]");
                        await fastClick("//button[contains(@class,'order-action-btn') and contains(@class,'order-action-btn--white-text') and .//span[contains(@class,'v-btn__content') and contains(normalize-space(),'注文確定')]]");
                        
                        return { success: true, logs };
                } catch (e) {
                        return { error: e.message, logs };
                }
                }
            """)

            # Process result
            for msg in result.get('logs', []):
                self.add_log(msg)
            if result.get('error'):
                error_map = {
                'not_available': '⚠ 商品が販売可能ではありません',
                'Timeout': '❌ タイムアウト',
                'checkbox_not_found': '❌ 受取店舗のチェックボックスが見つかりません'
                }
                self.add_log(error_map.get(result['error'], f"print: {result['error']}"))
                return None

            if result.get('success'):
                self.add_log("✓ テスト購入完了")
                return {'test_num': test_num, 'success': True}

            return None

        except Exception as e:
            self.add_log(f"❌ テスト実行エラー: {str(e)}")
            return None


    def __init__(self):
        super().__init__()
        # Frameless but visible in taskbar + Always on top
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.always_on_top = True

        self.border_thickness = 3
        self.header_height = 40
        self.drag_pos = None
        self.is_maximized = False

        # Data file path - handle both development and packaged executable
        if getattr(sys, 'frozen', False):
            # Running as compiled executable - use directory where .exe is located
            # This allows the app to read initial data from bundle and write updates next to .exe
            if hasattr(sys, '_MEIPASS'):
                # Initial data is in the temp bundle directory
                self._bundled_data_file = os.path.join(sys._MEIPASS, "data.json")
            else:
                self._bundled_data_file = None
            # Runtime data file goes next to the executable
            self.data_file = os.path.join(os.path.dirname(sys.executable), "data.json")
            # Copy initial data.json to exe directory if it doesn't exist
            if self._bundled_data_file and os.path.exists(self._bundled_data_file) and not os.path.exists(self.data_file):
                try:
                    import shutil
                    shutil.copy2(self._bundled_data_file, self.data_file)
                except Exception as e:
                    print(f"Warning: Could not copy initial data.json: {e}")
        else:
            # Running as script - use current directory
            self.data_file = "data.json"
            self._bundled_data_file = None

        # Browser instance (Playwright) - Purchase browser (logged in)
        self.playwright = None
        self.shop_browser = None  # Playwright browser (purchase)
        self.shop_context = None  # Playwright context
        self.shop_page = None     # Playwright page (purchase)
        self.browser_thread_id = None  # Track which thread owns the browser
        
        # Detection browsers (guest mode, no login) - support multiple browsers
        self.detection_browsers = []  # List of detection browser objects: [playwright, browser, context, page, loop, url, name]
        self.detection_loop = None     # Event loop for async operations (legacy, may be removed)
        
        # Legacy single browser attributes (for backward compatibility)
        self.detection_playwright = None
        self.detection_browser = None
        self.detection_context = None
        self.detection_page = None
        
        # Purchase queue: detection browser puts URLs here, purchase browser reads from here
        self.purchase_queue = queue.Queue()
        self.processed_urls_lock = threading.Lock()  # Lock for thread-safe URL tracking
        
        self.monitoring_active = False
        self.monitor_thread = None  # Track monitoring thread so we know when previous run fully stopped
        self.detection_thread = None  # Thread for detection browser
        self.purchase_thread = None  # Thread for purchase browser
        self.editing_row = None  # Track which row is being edited
        self.manual_auth_done = False  # Track if manual auth has been done at least once
        self.manual_auth_session_completed = False  # Track if manual auth browser session completed (skip login in monitoring)

        # Event to signal thread to continue from manual auth to monitoring
        self.continue_to_monitoring = threading.Event()
        self.browser_ready_for_monitoring = False  # Flag to indicate browser is ready and paused

        # Queue for browser thread tasks (like speed tests)
        self.browser_task_queue = queue.Queue()
        self.speed_test_running = False

        # Speed test data
        self.speed_test_results = []  # Store speed test results

        # Thread-safe signal emitter for background thread UI updates
        self.signal_emitter = SignalEmitter()
        self.signal_emitter.log_signal.connect(self._add_log_safe)
        self.signal_emitter.update_last_log_signal.connect(self._update_last_log_safe)
        self.signal_emitter.button_state_signal.connect(self._update_button_state_safe)
        self.signal_emitter.manual_auth_done_signal.connect(self._on_manual_auth_done)
        self.signal_emitter.show_auth_dialog_signal.connect(self._show_auth_dialog)
        self.signal_emitter.update_timer_signal.connect(self._update_timer_safe)

        # Auth confirmation dialog
        self.auth_dialog = None

        # Set window title & icon
        self.setWindowTitle("自動購入ツール")

        try:
            self.setWindowIcon(QIcon("app_icon.png"))  # ← replace with your icon file
        except Exception:
            pass  # Icon file may not exist

        self.init_ui()

        # Load saved login data and products
        self.load_login_data()
        self.load_products_from_json()

        # Set window size
        self.resize(450, 720)
        self.is_maximized = False

    def init_ui(self):
        # === Header Bar ===
        self.title_bar = QFrame()
        self.title_bar.setFixedHeight(self.header_height)
        self.title_bar.setStyleSheet("background-color: transparent;")

        # --- Icon + Title Text ---
        icon_label = QLabel()
        try:
            icon_pixmap = QIcon("app_icon.png").pixmap(30, 30)
            icon_label.setPixmap(icon_pixmap)
        except Exception:
            pass  # Icon file may not exist
        icon_label.setFixedSize(30, 30)

        title_label = QLabel("自動購入ツール")
        title_label.setStyleSheet("color: white; font-weight: bold; font-size: 17px;")
        title_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)

        # Combine icon + text
        title_container = QWidget()
        title_layout = QHBoxLayout(title_container)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(8)
        title_layout.addWidget(icon_label)
        title_layout.addWidget(title_label)

        # === Stylish Buttons ===
        self.btn_log_toggle = self.create_header_button("📋", "#E8E0D5", "#AA875F", hover_glow="#fac069")
        btn_pin = self.create_header_button("📌", "#E8E0D5", "#AA875F", hover_glow="#fac069")
        btn_min = self.create_header_button("–", "#F5E6CA", "#AA875F", hover_glow="#a19c94")
        btn_close = self.create_header_button("×", "#EAD7D1", "#AA4B3B", hover_glow="#FF6B6B")

        self.btn_log_toggle.clicked.connect(self.toggle_log_panel)
        btn_pin.clicked.connect(self.toggle_always_on_top)
        btn_min.clicked.connect(self.showMinimized)
        # btn_max.clicked.connect(self.toggle_max_restore)
        btn_close.clicked.connect(self.close)

        self.btn_pin = btn_pin  # Store reference for updating appearance

        # Set initial log toggle button style (inactive/gray since log is hidden)
        self.btn_log_toggle.setStyleSheet("""
            QPushButton {
                color: white;
                border: 2px solid #AA875F;
                border-radius: 15px;
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #AA875F,
                    stop:1 #6B7A76
                );
            }
            QPushButton:hover {
                background-color: #fac069;
                border: 2px solid white;
            }
            QPushButton:pressed {
                background-color: rgba(255,255,255,0.3);
            }
        """)

        # Set initial pin button style (active/green since always_on_top is True)
        btn_pin.setStyleSheet("""
            QPushButton {
                color: white;
                border: 2px solid #AA875F;
                border-radius: 15px;
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #4CAF50,
                    stop:1 #45a049
                );
            }
            QPushButton:hover {
                background-color: #fac069;
                border: 2px solid white;
            }
            QPushButton:pressed {
                background-color: rgba(255,255,255,0.3);
            }
        """)

        # Header layout
        header_layout = QHBoxLayout(self.title_bar)
        header_layout.setContentsMargins(10, 0, 8, 0)
        header_layout.addWidget(title_container)
        header_layout.addStretch()
        header_layout.addWidget(self.btn_log_toggle)
        header_layout.addWidget(btn_pin)
        header_layout.addWidget(btn_min)
        # header_layout.addWidget(btn_max)
        header_layout.addWidget(btn_close)

        # === Content Area ===
        content = QFrame()
        content.setStyleSheet("background-color: #F9F9F9; border: none;")

        # Main horizontal layout (log on left, content on right)
        main_content_layout = QHBoxLayout(content)
        main_content_layout.setContentsMargins(0, 0, 0, 0)
        main_content_layout.setSpacing(0)

        # === Left Panel: Log Window ===
        self.log_panel = QFrame()
        self.log_panel.setStyleSheet("background-color: #F9F9F9;")
        self.log_panel.setVisible(False)  # Hidden by default

        log_panel_layout = QVBoxLayout(self.log_panel)
        log_panel_layout.setContentsMargins(10, 10, 10, 10)
        log_panel_layout.setSpacing(5)

        # === Right Panel: Main Content ===
        right_panel = QFrame()
        right_panel.setStyleSheet("background-color: #F9F9F9; border: none;")
        content_layout = QVBoxLayout(right_panel)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(10)

        # === Login Form (Top) ===
        login_group = QGroupBox("アカウント情報")
        login_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 17px;
                color: #333;
                border: 2px solid #AA875F;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        login_group.setMaximumHeight(150)  # Control form height (adjust this value as needed)
        login_layout = QVBoxLayout()
        login_layout.setSpacing(8)

        # Email row - label and input parallel
        email_row = QHBoxLayout()
        email_row.setSpacing(8)
        email_label = QLabel("メールアドレス:")
        email_label.setStyleSheet("color: #333; font-size: 16px;")  # 1.2x larger
        email_label.setFixedWidth(120)  # Increased for larger font
        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("*****@gmail.com")
        self.email_input.setStyleSheet("""
            QLineEdit {
                padding: 5px;
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: white;
            }
            QLineEdit:focus {
                border: 1px solid #AA875F;
            }
        """)
        email_row.addWidget(email_label)
        email_row.addWidget(self.email_input, 1)

        # Warning label for email validation
        self.email_warning = QLabel("")
        self.email_warning.setStyleSheet("""
            color: #f44336;
            font-size: 12px;
            font-weight: bold;
            padding: 8px 10px;
            background-color: #ffebee;
            border-left: 4px solid #f44336;
            border-radius: 3px;
        """)
        self.email_warning.setWordWrap(True)
        self.email_warning.setVisible(False)

        # Password row - label and input parallel
        password_row = QHBoxLayout()
        password_row.setSpacing(8)
        password_label = QLabel("パスワード:")
        password_label.setStyleSheet("color: #333; font-size: 16px;")  # 1.2x larger
        password_label.setFixedWidth(120)  # Increased for larger font
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("パスワード")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setStyleSheet("""
            QLineEdit {
                padding: 5px;
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: white;
            }
            QLineEdit:focus {
                border: 1px solid #AA875F;
            }
        """)
        password_row.addWidget(password_label)
        password_row.addWidget(self.password_input, 1)

        # Save and Change buttons
        login_btn_layout = QHBoxLayout()
        login_btn_layout.setSpacing(8)

        self.save_login_btn = QPushButton("保存")
        self.save_login_btn.setFixedHeight(36)
        self.save_login_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 8px 15px;
                font-weight: bold;
                font-size: 16px;
                width: 300px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)

        self.change_login_btn = QPushButton("変更")
        self.change_login_btn.setFixedHeight(36)
        self.change_login_btn.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 8px 15px;
                font-weight: bold;
                font-size: 16px;
                width: 300px;
            }
            QPushButton:hover {
                background-color: #e68900;
            }
            QPushButton:pressed {
                background-color: #cc7a00;
            }
        """)

        self.clear_login_btn = QPushButton("クリア")
        self.clear_login_btn.setFixedHeight(36)
        self.clear_login_btn.setStyleSheet("""
            QPushButton {
                background-color: #9E9E9E;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 8px 15px;
                font-weight: bold;
                font-size: 16px;
                width: 300px;
            }
            QPushButton:hover {
                background-color: #757575;
            }
            QPushButton:pressed {
                background-color: #616161;
            }
        """)

        login_btn_layout.addWidget(self.save_login_btn)
        login_btn_layout.addWidget(self.change_login_btn)
        login_btn_layout.addWidget(self.clear_login_btn)
        login_btn_layout.addStretch()

        login_layout.addLayout(email_row)
        login_layout.addWidget(self.email_warning)
        login_layout.addLayout(password_row)
        login_layout.addLayout(login_btn_layout)
        login_group.setLayout(login_layout)

        # === Product Add Form ===
        search_group = QGroupBox("商品登録")
        search_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 17px;
                color: #333;
                border: 2px solid #AA875F;
                border-radius: 5px;
                margin-top: 3px;
                padding-top: 8px;
                padding-bottom: 5px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        search_group.setMaximumHeight(140)  # Control form height
        search_layout = QVBoxLayout()
        search_layout.setSpacing(8)
        search_layout.setContentsMargins(8, 8, 8, 8)

        # === Input Field Width Control ===
        # Adjust these values to change input field widths:
        PRODUCT_NAME_WIDTH = 0  # 0 = flexible (fills remaining space), or set fixed pixel value (e.g., 150)
        MIN_PRICE_WIDTH = 80    # Width in pixels (adjust as needed: 50, 70, 90, etc.)
        MAX_PRICE_WIDTH = 80    # Width in pixels (adjust as needed: 50, 70, 90, etc.)

        # Single row: Product name, min price, max price in order
        input_row = QHBoxLayout()
        input_row.setSpacing(6)

        # Product name input
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("商品名")
        if PRODUCT_NAME_WIDTH > 0:
            self.search_input.setFixedWidth(PRODUCT_NAME_WIDTH)
        self.search_input.setStyleSheet("""
            QLineEdit {
                padding: 6px;
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: white;
                font-size: 16px;
            }
            QLineEdit:focus {
                border: 1px solid #AA875F;
            }
        """)

        # Min price input
        self.min_price_input = QLineEdit()
        self.min_price_input.setPlaceholderText("下限")
        self.min_price_input.setFixedWidth(MIN_PRICE_WIDTH)
        self.min_price_input.setStyleSheet("""
            QLineEdit {
                padding: 6px;
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: white;
                font-size: 16px;
            }
            QLineEdit:focus {
                border: 1px solid #AA875F;
            }
        """)

        # Max price input
        self.max_price_input = QLineEdit()
        self.max_price_input.setPlaceholderText("上限")
        self.max_price_input.setFixedWidth(MAX_PRICE_WIDTH)
        self.max_price_input.setStyleSheet("""
            QLineEdit {
                padding: 6px;
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: white;
                font-size: 16px;
            }
            QLineEdit:focus {
                border: 1px solid #AA875F;
            }
        """)

        # Add to layout (product name gets stretch if width is 0)
        if PRODUCT_NAME_WIDTH == 0:
            input_row.addWidget(self.search_input, 1)  # Flexible width
        else:
            input_row.addWidget(self.search_input)  # Fixed width
        input_row.addWidget(self.min_price_input)
        input_row.addWidget(self.max_price_input)

        # Save button row (below inputs)
        save_button_row = QHBoxLayout()
        self.search_btn = QPushButton("保存")
        self.search_btn.setFixedHeight(36)
        self.search_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 8px 15px;
                font-weight: bold;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)
        save_button_row.addWidget(self.search_btn)

        # Add rows to search_layout
        search_layout.addLayout(input_row)
        search_layout.addLayout(save_button_row)
        search_group.setLayout(search_layout)

        # === Monitor Buttons (Outside Form) ===
        monitor_buttons_layout = QVBoxLayout()
        monitor_buttons_layout.setSpacing(8)

        # Start button - blue, starts browser and shows dialog
        self.manual_auth_btn = QPushButton("スタート")
        self.manual_auth_btn.setFixedHeight(50)
        self.manual_auth_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #42A5F5, stop:1 #1E88E5);
                color: #fff;
                border-radius: 8px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 16px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #64B5F6, stop:1 #1976D2);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1976D2, stop:1 #1565C0);
            }
        """)

        # Hint label for manual auth
        # self.manual_auth_hint = QLabel("※初回は手動購入で認証後、監視開始")
        # self.manual_auth_hint.setStyleSheet("""
        #     QLabel {
        #         color: #666;
        #         font-size: 11px;
        #         padding: 2px 0px;
        #     }
        # """)
        # self.manual_auth_hint.setWordWrap(True)

        # Horizontal layout for start/stop buttons
        start_stop_layout = QHBoxLayout()
        start_stop_layout.setSpacing(10)

        # Start button - large, bold, yellow
        self.start_monitor_btn = QPushButton("監視開始")
        self.start_monitor_btn.setFixedHeight(80)
        self.start_monitor_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #FFD54F, stop:1 #FFC107);
                color: #1a1a1a;
                border-radius: 10px;
                padding: 15px 30px;
                font-weight: 900;
                font-size: 24px;
                text-transform: uppercase;
                letter-spacing: 2px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #FFE082, stop:1 #FFB300);
                color: #000;
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #FFCA28, stop:1 #FFA000);
                padding: 16px 30px 14px 30px;
            }
        """)

        # Stop button - large, bold, red
        self.stop_monitor_btn = QPushButton("監視停止")
        self.stop_monitor_btn.setFixedHeight(80)
        self.stop_monitor_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #EF5350, stop:1 #da190b);
                color: #fff;
                border-radius: 10px;
                padding: 15px 30px;
                font-weight: 900;
                font-size: 24px;
                text-transform: uppercase;
                letter-spacing: 2px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #F44336, stop:1 #C62828);
                color: #fff;
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #D32F2F, stop:1 #c41409);
                padding: 16px 30px 14px 30px;
            }
        """)

        # Initially hide both monitoring buttons (until manual auth is done)
        self.start_monitor_btn.setVisible(False)
        self.stop_monitor_btn.setVisible(False)

        # Add buttons to start_stop_layout
        start_stop_layout.addWidget(self.start_monitor_btn)
        start_stop_layout.addWidget(self.stop_monitor_btn)

        # Add all elements to monitor_buttons_layout (vertical)
        monitor_buttons_layout.addWidget(self.manual_auth_btn)
        # monitor_buttons_layout.addWidget(self.manual_auth_hint)
        monitor_buttons_layout.addLayout(start_stop_layout)
        monitor_buttons_layout.addStretch()

        # === Product Table ===
        table_header_layout = QHBoxLayout()
        table_label = QLabel("商品一覧")
        table_label.setStyleSheet("color: #333; font-weight: bold; font-size: 17px;")

        self.clear_all_products_btn = QPushButton("全て削除")
        self.clear_all_products_btn.setFixedHeight(32)
        self.clear_all_products_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 5px 12px;
                font-weight: bold;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: #da190b;
            }
            QPushButton:pressed {
                background-color: #c41409;
            }
        """)
        self.clear_all_products_btn.clicked.connect(self.on_clear_all_products)

        table_header_layout.addWidget(table_label)
        table_header_layout.addStretch()
        table_header_layout.addWidget(self.clear_all_products_btn)

        # === Table Height Control ===
        # Adjust this value to change table height (3 rows ≈ 166px, 4 rows ≈ 220px, 5 rows ≈ 275px)
        TABLE_MAX_HEIGHT = 150  # Height in pixels for maximum table size

        self.product_table = QTableWidget()
        self.product_table.setColumnCount(5)
        self.product_table.setHorizontalHeaderLabels(["商品名", "下限価格", "上限価格", "変更", "削除"])
        self.product_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.product_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.product_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.product_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.product_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)

        # Set initial width for price columns
        self.product_table.setColumnWidth(1, 100)  # Min price column
        self.product_table.setColumnWidth(2, 100)  # Max price column

        # Enable word wrap to show full URLs without truncation
        self.product_table.setWordWrap(True)

        # Auto-resize rows to fit content
        self.product_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

        # Hide the default row numbers (vertical header)
        self.product_table.verticalHeader().setVisible(False)

        self.product_table.setStyleSheet("""
            QTableWidget {
                background-color: white;
                border: 1px solid #ccc;
                border-radius: 3px;
                gridline-color: #e0e0e0;
                font-size: 13px;
            }
            QTableWidget::item {
                padding: 5px;
            }
            QHeaderView::section {
                background-color: #AA875F;
                color: white;
                padding: 5px;
                border: none;
                font-weight: bold;
                font-size: 14px;
            }
        """)

        # Limit table height for 3 items maximum
        self.product_table.setMaximumHeight(TABLE_MAX_HEIGHT)

        # === Log Window ===
        # === Log Panel Content ===
        log_header_layout = QHBoxLayout()
        log_label = QLabel("ログ")
        log_label.setStyleSheet("color: #333; font-weight: bold; font-size: 14px;")
        log_header_layout.addWidget(log_label)
        log_header_layout.addStretch()

        self.log_window = QTextEdit()
        self.log_window.setReadOnly(True)
        self.log_window.setStyleSheet("""
            QTextEdit {
                background-color: #2b2b2b;
                color: #e0e0e0;
                border: 1px solid #ccc;
                border-radius: 3px;
                font-family: 'Segoe UI', 'Meiryo UI', 'MS Gothic', Consolas, monospace;
                font-size: 13px;
                font-weight: 500;
                padding: 8px;
                line-height: 1.4;
                letter-spacing: 0.3px;
            }
            /* Modern thin scrollbar */
            QScrollBar:vertical {
                background: #1a1a1a;
                width: 8px;
                border-radius: 4px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #4a4a4a;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #5a5a5a;
            }
            QScrollBar::handle:vertical:pressed {
                background: #6a6a6a;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: none;
            }
            /* Horizontal scrollbar */
            QScrollBar:horizontal {
                background: #1a1a1a;
                height: 8px;
                border-radius: 4px;
                margin: 0px;
            }
            QScrollBar::handle:horizontal {
                background: #4a4a4a;
                border-radius: 4px;
                min-width: 20px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #5a5a5a;
            }
            QScrollBar::handle:horizontal:pressed {
                background: #6a6a6a;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: none;
            }
        """)
        # Set font to improve rendering on Windows
        log_font = QFont("Segoe UI", 10)
        log_font.setStyleHint(QFont.SansSerif)
        log_font.setWeight(QFont.Medium)
        self.log_window.setFont(log_font)

        self.log_window.append("システム起動しました...")

        # Add log components to log panel
        log_panel_layout.addLayout(log_header_layout)
        log_panel_layout.addWidget(self.log_window, 1)

        # Create horizontal layout for Product Registration (70%) and Monitor buttons (30%)
        registration_monitor_layout = QHBoxLayout()
        registration_monitor_layout.setSpacing(10)
        registration_monitor_layout.addWidget(search_group, 7)  # 70% width

        # Monitor buttons container (vertically centered)
        monitor_container = QWidget()
        monitor_container_layout = QVBoxLayout(monitor_container)
        monitor_container_layout.setContentsMargins(0, 0, 0, 0)
        monitor_container_layout.addStretch(1)  # Top stretch for centering
        monitor_container_layout.addLayout(monitor_buttons_layout)
        monitor_container_layout.addStretch(1)  # Bottom stretch for centering

        registration_monitor_layout.addWidget(monitor_container, 3)  # 30% width

        # === Speed Test Section ===
        speed_test_group = QGroupBox("スピードテスト")
        speed_test_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 17px;
                color: #333;
                border: 2px solid #AA875F;
                border-radius: 5px;
                margin-top: 3px;
                padding-top: 8px;
                padding-bottom: 5px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        speed_test_group.setMaximumHeight(110)

        speed_test_layout = QVBoxLayout()
        speed_test_layout.setSpacing(8)
        speed_test_layout.setContentsMargins(8, 8, 8, 8)

        # URL input row
        url_row = QHBoxLayout()
        url_row.setSpacing(6)

        url_label = QLabel("商品URL:")
        url_label.setStyleSheet("color: #333; font-size: 14px; font-weight: normal;")
        url_label.setFixedWidth(80)

        self.speed_test_url_input = QLineEdit()
        self.speed_test_url_input.setPlaceholderText("https://shop.kitamura.jp/ec/used/...")
        self.speed_test_url_input.setStyleSheet("""
            QLineEdit {
                padding: 6px;
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: white;
                font-size: 13px;
            }
            QLineEdit:focus {
                border: 1px solid #AA875F;
            }
        """)

        url_row.addWidget(url_label)
        url_row.addWidget(self.speed_test_url_input, 1)

        # Buttons row
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(8)

        self.speed_test_start_btn = QPushButton("テスト開始")
        self.speed_test_start_btn.setFixedHeight(32)
        self.speed_test_start_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 5px 15px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton:pressed {
                background-color: #1565C0;
            }
        """)

        self.speed_test_clear_btn = QPushButton("結果クリア")
        self.speed_test_clear_btn.setFixedHeight(32)
        self.speed_test_clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #9E9E9E;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 5px 15px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #757575;
            }
            QPushButton:pressed {
                background-color: #616161;
            }
        """)

        buttons_row.addWidget(self.speed_test_start_btn)
        buttons_row.addWidget(self.speed_test_clear_btn)
        buttons_row.addStretch()

        speed_test_layout.addLayout(url_row)
        speed_test_layout.addLayout(buttons_row)
        speed_test_group.setLayout(speed_test_layout)

        # Add all components to right panel content layout
        content_layout.addWidget(login_group)
        content_layout.addLayout(registration_monitor_layout)  # Parallel layout
        content_layout.addWidget(speed_test_group)  # Speed test section
        content_layout.addLayout(table_header_layout)
        content_layout.addWidget(self.product_table, 1)

        # Add panels to main horizontal layout (stacked, only one visible at a time)
        main_content_layout.addWidget(self.log_panel, 1)
        main_content_layout.addWidget(right_panel, 1)

        # Store reference to right panel for toggling
        self.right_panel = right_panel

        # Ensure main window is visible, log is hidden at startup
        self.right_panel.setVisible(True)
        self.log_panel.setVisible(False)

        # === Main Layout ===
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(
            self.border_thickness, self.border_thickness,
            self.border_thickness, self.border_thickness
        )
        main_layout.setSpacing(0)
        main_layout.addWidget(self.title_bar)
        main_layout.addWidget(content, 1)

        self.setMinimumSize(500, 550)

        # Connect button signals
        self.search_btn.clicked.connect(self.on_search)
        self.manual_auth_btn.clicked.connect(self.on_manual_auth)
        self.start_monitor_btn.clicked.connect(self.on_start_monitor)
        self.stop_monitor_btn.clicked.connect(self.on_stop_monitor)
        self.save_login_btn.clicked.connect(self.on_save_login)
        self.change_login_btn.clicked.connect(self.on_change_login)
        self.clear_login_btn.clicked.connect(self.on_clear_login)

        # Connect email input to hide warning when user starts typing
        self.email_input.textChanged.connect(self.on_email_changed)

        # Connect speed test buttons
        self.speed_test_start_btn.clicked.connect(self.on_speed_test_start)
        self.speed_test_clear_btn.clicked.connect(self.on_speed_test_clear)

        # Initially enable input fields for first-time entry
        self.email_input.setReadOnly(False)
        self.password_input.setReadOnly(False)
        self.save_login_btn.setEnabled(True)

    def on_change_product(self, row):
        """Handle change button click - load row data into form for editing"""
        try:
            # Get data from the row
            name_item = self.product_table.item(row, 0)
            min_price_item = self.product_table.item(row, 1)
            max_price_item = self.product_table.item(row, 2)

            if name_item:
                # Load product name
                product_name = name_item.text()
                self.search_input.setText(product_name)

                # Load min price
                min_price = min_price_item.data(Qt.UserRole) if min_price_item else None
                if min_price is not None:
                    self.min_price_input.setText(str(min_price))
                else:
                    self.min_price_input.clear()

                # Load max price
                max_price = max_price_item.data(Qt.UserRole) if max_price_item else None
                if max_price is not None:
                    self.max_price_input.setText(str(max_price))
                else:
                    self.max_price_input.clear()

                # Track which row is being edited
                self.editing_row = row

                # Focus on the first input
                self.search_input.setFocus()
                self.search_input.selectAll()

                self.add_log(f"📝 編集モード: {product_name}")

        except Exception as e:
            self.add_log(f"❌ 変更エラー: {str(e)}")
            print(f"Error in on_change_product: {str(e)}")

    def on_start_monitor(self):
        """Handle monitor start button click - signal the waiting thread to continue"""
        # Check if browser is ready and waiting
        if not self.browser_ready_for_monitoring:
            self.add_log("❌ まず「スタート」ボタンでブラウザを起動してください")
            return

        # Check if there are products in the table to monitor
        product_count = self.product_table.rowCount()
        if product_count == 0:
            self.add_log("⚠ 監視する商品を追加してください")
            return

        self.add_log(f"✓ {product_count}件の商品の監視を開始します")
        self.add_log(f"📄 監視ページ: https://shop.kitamura.jp/ec/list?type=u&sort=update_date&limit=40")

        # Print product filters to console
        products = self.get_products_for_monitoring()
        print("\n" + "=" * 80)
        print("📋 監視中の商品フィルター (Product Filters)")
        print("=" * 80)
        for idx, product in enumerate(products, 1):
            price_range = ""
            if product.get('min_price') and product.get('max_price'):
                price_range = f" ({product['min_price']:,}¥ ~ {product['max_price']:,}¥)"
            elif product.get('min_price'):
                price_range = f" ({product['min_price']:,}¥以上)"
            elif product.get('max_price'):
                price_range = f" ({product['max_price']:,}¥以下)"
            print(f"{idx}. {product['name']}{price_range}")
        print("=" * 80 + "\n")

        # Change button visibility
        self.start_monitor_btn.setVisible(False)
        self.stop_monitor_btn.setVisible(True)

        # Set monitoring active and signal the waiting thread to continue
        self.monitoring_active = True
        self.continue_to_monitoring.set()  # Signal the thread to continue

    def get_products_for_monitoring(self):
        """Get all products from table for monitoring"""
        products = []
        product_count = self.product_table.rowCount()

        for row in range(product_count):
            name_item = self.product_table.item(row, 0)
            min_price_item = self.product_table.item(row, 1)
            max_price_item = self.product_table.item(row, 2)

            if name_item:
                name = name_item.text()
                min_price = min_price_item.data(Qt.UserRole) if min_price_item else None
                max_price = max_price_item.data(Qt.UserRole) if max_price_item else None

                if name:
                    products.append({
                        'name': name,
                        'min_price': min_price,
                        'max_price': max_price
                    })

        return products

    def on_stop_monitor(self):
        """Handle stop monitor button click - close the application"""
        try:
            self.add_log("🛑 アプリケーションを終了します...")
            # Close the application (triggers closeEvent for proper cleanup)
            self.close()
        except Exception as e:
            self.add_log(f"❌ 停止ボタンエラー: {str(e)}")
            import traceback
            print(f"Stop button error: {traceback.format_exc()}")

    def on_manual_auth(self):
        """Handle manual auth button click - open browser for manual purchase (phone verification bypass)"""
        # Check if browser library is available
        if not BROWSER_AVAILABLE:
            self.add_log("❌ ブラウザライブラリがインストールされていません")
            self.add_log("   → pip install playwright を実行してください")
            self.add_log("   → playwright install chromium を実行してください")
            return

        # Check if login data is saved in JSON file
        if not self.has_saved_login():
            self.add_log("❌ ログイン情報が必要です")
            self.add_log("   → 上のフォームでメールとパスワードを入力し、「保存」ボタンを押してください")
            self.email_input.setFocus()
            return

        # Check if thread is already running
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.add_log("⚠ 既に処理中です")
            return

        self.add_log(">")
        self.add_log("🚀 スタートボタンが押されました")
        self.add_log(">")

        # Show dialog immediately (before browser starts)
        self.browser_ready_for_monitoring = True  # Set flag so dialog can work
        self._show_auth_dialog()

        self.add_log("🌐 ブラウザを起動中...")

        # Start browser in a separate thread
        self.monitor_thread = threading.Thread(target=self.start_manual_auth_browser)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()

    def start_manual_auth_browser(self):
        """Start browser for manual authentication (no automatic monitoring)"""
        try:
            profile_path = "C:\\playwright\\AutomationProfile"

            # Check if browser is already running
            browser_already_running = False
            if self.shop_page is not None:
                try:
                    _ = self.shop_page.url
                    browser_already_running = True
                    self.add_log("✓ 既存のブラウザを使用します")
                except Exception:
                    # Browser was closed, need to restart
                    try:
                        if self.shop_context:
                            self.shop_context.close()
                        if self.shop_browser:
                            self.shop_browser.close()
                        if self.playwright:
                            self.playwright.stop()
                    except Exception:
                        pass
                    self.shop_page = None
                    self.shop_context = None
                    self.shop_browser = None
                    self.playwright = None

            if not browser_already_running:
                self.add_log("🌐 ブラウザを起動中...")

                # Start Playwright
                self.playwright = sync_playwright().start()

                # Launch browser with persistent context (prefer bundled Chromium, fall back to installed browsers)
                launch_kwargs = {
                    "user_data_dir": profile_path,
                    "headless": False,
                    "slow_mo": 100,  # Slight delay for stability
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                    ],
                }
                browser_started = False

                try:
                    self.shop_browser = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
                    browser_started = True
                    self.add_log("✓ Playwright Chromiumを使用します")
                except Exception as e_primary:
                    self.add_log("⚠ PlaywrightのChromiumが見つかりません。ダウンロード済みか確認してください。")
                    self.add_log("   → playwright install chromium を実行してください")
                    self.add_log("   → 既存ブラウザ(Edge/Chrome)で再試行します...")

                    # Try Microsoft Edge channel
                    if not browser_started:
                        try:
                            self.shop_browser = self.playwright.chromium.launch_persistent_context(
                                channel="msedge", **launch_kwargs
                            )
                            browser_started = True
                            self.add_log("✓ Microsoft Edge を使用して起動しました")
                        except Exception:
                            pass

                    # Try Google Chrome channel
                    if not browser_started:
                        try:
                            self.shop_browser = self.playwright.chromium.launch_persistent_context(
                                channel="chrome", **launch_kwargs
                            )
                            browser_started = True
                            self.add_log("✓ Google Chrome を使用して起動しました")
                        except Exception as e_fallback:
                            self.add_log(f"❌ ブラウザ起動に失敗しました: {str(e_fallback)}")
                            try:
                                if self.playwright:
                                    self.playwright.stop()
                            finally:
                                self.playwright = None
                            return

                # Store the thread ID that owns this browser
                self.browser_thread_id = threading.current_thread().ident

                # Wait for browser to start
                self.add_log("⏳ ブラウザの起動を待っています...")
                time.sleep(0.5)

                try:
                    if len(self.shop_browser.pages) > 0:
                        self.shop_page = self.shop_browser.pages[0]
                    else:
                        self.shop_page = self.shop_browser.new_page()

                    self.shop_page.set_default_timeout(60000)
                    self.add_log("✓ ブラウザを起動しました")
                except Exception as e:
                    self.add_log(f"❌ ブラウザに接続できませんでした: {str(e)}")
                    return

            # Navigate to shop homepage
            try:
                self.add_log("📄 ショップページを開いています...")
                self.shop_page.goto("https://shop.kitamura.jp", wait_until="domcontentloaded")
                time.sleep(0.5)

                # Check if login is needed
                page_html = self.shop_page.content()
                needs_login = "login-user-email" in page_html or "新規登録" in page_html or "ログイン" in page_html

                if needs_login and "login-user-email" not in page_html:
                    # Try to click login button with multiple strategies
                    self.add_log("🔍 ログインボタンを探しています...")
                    click_successful = False
                    max_click_attempts = 3

                    for attempt in range(1, max_click_attempts + 1):
                        try:
                            login_element = None

                            # By link text
                            if not login_element:
                                try:
                                    login_element = self.shop_page.get_by_text("ログイン", exact=False).first
                                    login_element.wait_for(state="visible", timeout=5000)
                                except:
                                    login_element = None

                            if login_element:
                                login_element.click()
                                time.sleep(0.1)
                                page_html = self.shop_page.content()
                                if "login-user-email" in page_html:
                                    click_successful = True
                                    break
                        except Exception:
                            if attempt < max_click_attempts:
                                time.sleep(0.2)

                # Fill login form if visible
                page_html = self.shop_page.content()
                if "login-user-email" in page_html:
                    self.add_log("📧 ログイン情報を入力中...")

                    # Get saved credentials
                    if os.path.exists(self.data_file):
                        with open(self.data_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            login_data = data.get('login', {})
                            saved_email = login_data.get('email', '')
                            saved_password = login_data.get('password', '')

                            if saved_email and saved_password:
                                # Fill email with retry and verification
                                max_input_attempts = 3
                                email_filled = False

                                for attempt in range(1, max_input_attempts + 1):
                                    try:
                                        email_input = self.shop_page.locator("#login-user-email")
                                        email_input.wait_for(state="visible", timeout=5000)
                                        email_input.fill("")
                                        time.sleep(0.1)
                                        email_input.fill(saved_email)
                                        time.sleep(0.1)

                                        # Verify the email was entered correctly
                                        entered_value = email_input.input_value()
                                        if entered_value == saved_email:
                                            self.add_log("✓ メールアドレスを入力しました")
                                            email_filled = True
                                            break
                                    except Exception as e:
                                        if attempt < max_input_attempts:
                                            time.sleep(0.1)

                                # Fill password with retry and verification
                                password_filled = False

                                for attempt in range(1, max_input_attempts + 1):
                                    try:
                                        password_input = self.shop_page.locator("#login-user-password")
                                        password_input.wait_for(state="visible", timeout=5000)
                                        password_input.fill("")
                                        time.sleep(0.1)
                                        password_input.fill(saved_password)
                                        time.sleep(0.1)

                                        # Verify password length
                                        entered_value = password_input.input_value()
                                        if len(entered_value) == len(saved_password):
                                            self.add_log("✓ パスワードを入力しました")
                                            password_filled = True
                                            break
                                    except Exception as e:
                                        if attempt < max_input_attempts:
                                            time.sleep(0.1)

                                # Click login button with retry
                                max_button_attempts = 3
                                for attempt in range(1, max_button_attempts + 1):
                                    try:
                                        login_button = None

                                        # Strategy 1: By ID
                                        try:
                                            login_button = self.shop_page.locator("#login-button")
                                            login_button.wait_for(state="visible", timeout=5000)
                                        except:
                                            login_button = None

                                        if login_button:
                                            try:
                                                login_button.evaluate("element => element.click()")
                                            except:
                                                login_button.click()
                                            self.add_log("✓ ログインボタンをクリックしました")
                                            break
                                    except Exception as e:
                                        if attempt < max_button_attempts:
                                            time.sleep(0.2)

                                # Wait for login to complete
                                self.add_log("⏳ ログイン処理中...")
                                time.sleep(1)

                                # Verify login success
                                max_verify_attempts = 10
                                for attempt in range(max_verify_attempts):
                                    time.sleep(0.3)
                                    page_html = self.shop_page.content()
                                    if "login-user-email" not in page_html:
                                        self.add_log("✓ ログイン成功しました")
                                        break

                self.add_log(">")
                self.add_log("✅ ブラウザの準備が完了しました")
                self.add_log(">")
                self.add_log("📱 手動で商品を選んで購入してください")
                self.add_log("📱 完了後ダイアログの「確認」ボタンを押してください")
                self.add_log("⚠ ブラウザを閉じないでください！")
                self.add_log(">")

                # Dialog is already shown from on_manual_auth
                # Just log that browser is ready

                # Wait for signal to continue with monitoring (or process speed test tasks)
                self.add_log("⏸ 監視開始ボタンを待機中...")
                while not self.continue_to_monitoring.is_set():
                    # Check for speed test tasks while waiting
                    try:
                        task = self.browser_task_queue.get(timeout=0.5)
                        if task and task['type'] == 'speed_test':
                            self._execute_speed_test_task(task['url'])
                    except queue.Empty:
                        pass  # No tasks, continue waiting

                # Check if we should continue or stop
                if not self.monitoring_active:
                    self.add_log("⚠ 監視がキャンセルされました")
                    return

                # Get products from table (need to get from main thread)
                products = self.get_products_for_monitoring()
                if not products:
                    self.add_log("⚠ 監視する商品がありません")
                    return

                # Start detection browser (guest mode, no login)
                # Run in a separate thread to avoid event loop conflicts
                try:
                    import queue as thread_queue
                    
                    result_queue = thread_queue.Queue()
                    
                    def run_browser_startup():
                        """Run async browser startup in a fresh thread with no event loop"""
                        try:
                            # Create a new event loop in this thread (no conflicts possible)
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            
                            # Prepare URLs for multiple browsers (1 new arrivals + 3 individual products)
                            urls_and_names = []
                            
                            # New arrivals page
                            FIXED_MONITORING_URL = "https://shop.kitamura.jp/ec/list?type=u&sort=update_date&limit=40"
                            urls_and_names.append((FIXED_MONITORING_URL, "新着ページ検知", products))
                            
                            # Individual product pages (up to 3)
                            for idx, product in enumerate(products[:3]):  # Max 3 products
                                product_name = product.get('name', '').strip()
                                if product_name:
                                    search_url = f"https://shop.kitamura.jp/ec/list?keyword={product_name.replace(' ', '+')}&type=u"
                                    single_product_filter = [product]
                                    urls_and_names.append((search_url, f"{product_name}検知", single_product_filter))
                            
                            # Run the async function to start multiple browsers
                            result = loop.run_until_complete(self._start_multiple_detection_browsers(urls_and_names))
                            
                            # Store the loop for later operations (same thread will use it)
                            result_queue.put(('success', result, loop))
                        except Exception as e:
                            result_queue.put(('error', e, None))
                    
                    # Run in a separate thread (this thread has no event loop)
                    browser_thread = threading.Thread(target=run_browser_startup)
                    browser_thread.daemon = True
                    browser_thread.start()
                    browser_thread.join()  # Wait for completion
                    
                    # Get the result
                    try:
                        status, value, loop = result_queue.get_nowait()
                    except thread_queue.Empty:
                        self.add_log("❌ 検知ブラウザの起動に失敗しました（タイムアウト）")
                        return
                    
                    if status == 'error':
                        raise value
                    
                    if not value:
                        self.add_log("❌ 検知ブラウザの起動に失敗しました")
                        return
                    
                    # Store the loop for later async operations
                    # Note: The loop is thread-local, but we store the reference
                    # The detection monitoring will run in a different thread, so we'll need
                    # to create a new loop there or use the same thread
                    if loop:
                        self.detection_loop = loop
                        
                except Exception as e:
                    self.add_log(f"❌ 検知ブラウザの起動に失敗しました: {str(e)}")
                    import traceback
                    self.add_log(f"詳細: {traceback.format_exc()}")
                    return

                self.add_log(">")
                self.add_log("🚀 自動監視を開始します（2ブラウザモード）")
                self.add_log(">")

                # Continue with monitoring logic (both browsers are now open)
                self._continue_monitoring_with_browser(products)

            except Exception as e:
                self.add_log(f"❌ ページ読み込みエラー: {str(e)}")
                import traceback
                self.add_log(f"詳細: {traceback.format_exc()}")

        except Exception as e:
            self.add_log(f"❌ 手動認証エラー: {str(e)}")
            import traceback
            self.add_log(f"詳細: {traceback.format_exc()}")
        finally:
            # Always reset state when thread ends
            self.browser_ready_for_monitoring = False
            self.continue_to_monitoring.clear()
            self.monitoring_active = False
            self.signal_emitter.button_state_signal.emit(False)

    def on_save_login(self):
        """Handle save login button click"""
        email = self.email_input.text()
        password = self.password_input.text()

        if not email or not password:
            self.add_log("⚠ メールアドレスとパスワードを入力してください")
            return

        # Validate email format before saving (show warning if invalid)
        if not self.validate_email(show_warning=True):
            self.add_log("❌ 正しいGmailアドレスの形式で入力してください")
            return

        # Save login data to JSON file
        if self.save_login_data(email, password):
            # Lock the input fields
            self.email_input.setReadOnly(True)
            self.password_input.setReadOnly(True)
            self.save_login_btn.setEnabled(False)

            self.add_log("💾 ログイン情報を保存しました")
        else:
            self.add_log("❌ ログイン情報の保存に失敗しました")

    def on_change_login(self):
        """Handle change login button click"""
        # Unlock the input fields for editing
        self.email_input.setReadOnly(False)
        self.password_input.setReadOnly(False)
        self.save_login_btn.setEnabled(True)
        self.email_input.setFocus()

        self.add_log("✏️ ログイン情報を編集モードにしました")

    def on_clear_login(self):
        """Handle clear login button click - DELETE operation"""
        # Clear input fields
        self.email_input.clear()
        self.password_input.clear()

        # Enable editing
        self.email_input.setReadOnly(False)
        self.password_input.setReadOnly(False)
        self.save_login_btn.setEnabled(True)

        # Delete from JSON
        if self.delete_login_from_json():
            self.add_log("🗑️ ログイン情報をクリアしました")
            self.email_input.setFocus()
        else:
            self.add_log("⚠ ログイン情報のクリアに失敗しました")

    def on_email_changed(self):
        """Handle email input text change - reset styling to normal"""
        # Hide warning and reset to normal style when user is typing
        self.email_warning.setVisible(False)
        self.email_input.setStyleSheet("""
            QLineEdit {
                padding: 5px;
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: white;
            }
            QLineEdit:focus {
                border: 1px solid #AA875F;
            }
        """)

    def validate_email(self, show_warning=True):
        """Validate email format and optionally show warning"""
        email = self.email_input.text()

        # If empty, return False (invalid)
        if not email:
            if show_warning:
                self.email_warning.setVisible(False)
                self.email_input.setStyleSheet("""
                    QLineEdit {
                        padding: 5px;
                        border: 1px solid #ccc;
                        border-radius: 3px;
                        background-color: white;
                    }
                    QLineEdit:focus {
                        border: 1px solid #AA875F;
                    }
                """)
            return False

        # Gmail format: username@gmail.com
        # Allow letters, numbers, dots, and underscores in username
        gmail_pattern = r'^[a-zA-Z0-9._]+@gmail\.com$'

        if re.match(gmail_pattern, email):
            # Valid Gmail format
            if show_warning:
                self.email_warning.setVisible(False)
                self.email_input.setStyleSheet("""
                    QLineEdit {
                        padding: 5px;
                        border: 2px solid #4CAF50;
                        border-radius: 3px;
                        background-color: white;
                    }
                    QLineEdit:focus {
                        border: 2px solid #4CAF50;
                    }
                """)
            return True
        else:
            # Invalid format
            if show_warning:
                self.email_input.setStyleSheet("""
                    QLineEdit {
                        padding: 5px;
                        border: 2px solid #f44336;
                        border-radius: 3px;
                        background-color: #fff5f5;
                    }
                    QLineEdit:focus {
                        border: 2px solid #f44336;
                    }
                """)
                self.email_warning.setText("⚠ 正しいGmailアドレスの形式で入力してください\n例: *****@gmail.com")
                self.email_warning.setVisible(True)
            return False

    def add_log(self, message):
        """Add a log message to the log window (thread-safe)"""
        try:
            # Check if we're in the main thread
            if threading.current_thread() == threading.main_thread():
                self._add_log_safe(message)
            else:
                # Emit signal for background thread
                self.signal_emitter.log_signal.emit(message)
        except Exception as e:
            # Fallback: print to console if logging fails
            print(f"[LOG ERROR] {message} | Error: {str(e)}")

    def _add_log_safe(self, message):
        """Internal method to add log (must be called from main thread)"""
        try:
            from datetime import datetime
            if not hasattr(self, 'log_window') or self.log_window is None:
                return

            # Add timestamp and append message
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_window.append(f"[{timestamp}] {message}")

            # Limit log to maximum 300 lines to prevent memory issues
            MAX_LOG_LINES = 300

            # Get current document
            doc = self.log_window.document()

            # Check line count and remove old lines if exceeded
            if doc.blockCount() > MAX_LOG_LINES:
                # Calculate how many lines to remove
                lines_to_remove = doc.blockCount() - MAX_LOG_LINES

                # Create cursor at document start
                cursor = self.log_window.textCursor()
                cursor.movePosition(cursor.Start)

                # Select and delete old lines
                for _ in range(lines_to_remove):
                    cursor.select(cursor.BlockUnderCursor)
                    cursor.removeSelectedText()
                    cursor.deleteChar()  # Remove the newline

                # Move cursor to end (keep showing latest logs)
                cursor.movePosition(cursor.End)
                self.log_window.setTextCursor(cursor)

        except Exception as e:
            print(f"[LOG ERROR] {message} | Error: {str(e)}")

    def update_last_log(self, message):
        """Update the last log line (thread-safe) - for countdown display"""
        try:
            if threading.current_thread() == threading.main_thread():
                self._update_last_log_safe(message)
            else:
                self.signal_emitter.update_last_log_signal.emit(message)
        except Exception as e:
            print(f"[UPDATE LOG ERROR] {message} | Error: {str(e)}")

    def _update_last_log_safe(self, message):
        """Internal method to update last log line (must be called from main thread)"""
        try:
            from datetime import datetime
            if not hasattr(self, 'log_window') or self.log_window is None:
                return

            # Get cursor and move to end
            cursor = self.log_window.textCursor()
            cursor.movePosition(cursor.End)

            # Move to start of last line
            cursor.movePosition(cursor.StartOfBlock)

            # Select entire last line
            cursor.movePosition(cursor.EndOfBlock, cursor.KeepAnchor)

            # Replace with new content (with timestamp)
            timestamp = datetime.now().strftime("%H:%M:%S")
            cursor.insertText(f"[{timestamp}] {message}")

            # Scroll to bottom
            self.log_window.verticalScrollBar().setValue(
                self.log_window.verticalScrollBar().maximum()
            )

        except Exception as e:
            print(f"[UPDATE LOG ERROR] {message} | Error: {str(e)}")

    def _update_button_state_safe(self, is_monitoring):
        """Internal method to update button state (must be called from main thread)"""
        try:
            if not hasattr(self, 'start_monitor_btn') or not hasattr(self, 'stop_monitor_btn'):
                return
            if is_monitoring:
                self.start_monitor_btn.setVisible(False)
                self.stop_monitor_btn.setVisible(True)
            else:
                # Only show start button if manual auth has been done
                if self.manual_auth_done:
                    self.start_monitor_btn.setVisible(True)
                self.stop_monitor_btn.setVisible(False)
        except Exception as e:
            print(f"[BUTTON STATE ERROR] {str(e)}")

    def update_timer(self, elapsed_time):
        """Update the speed test timer (thread-safe)"""
        try:
            if threading.current_thread() == threading.main_thread():
                self._update_timer_safe(f"{elapsed_time:.3f}秒")
            else:
                self.signal_emitter.update_timer_signal.emit(f"{elapsed_time:.3f}秒")
        except Exception as e:
            print(f"[UPDATE TIMER ERROR] {elapsed_time} | Error: {str(e)}")

    def _update_timer_safe(self, time_text):
        """Internal method to update timer label (must be called from main thread)"""
        try:
            if not hasattr(self, 'speed_test_timer_label') or self.speed_test_timer_label is None:
                return
            self.speed_test_timer_label.setText(time_text)
        except Exception as e:
            print(f"[UPDATE TIMER SAFE ERROR] {time_text} | Error: {str(e)}")

    def _on_manual_auth_done(self):
        """Internal method to show monitoring button after manual auth (must be called from main thread)"""
        try:
            self.manual_auth_done = True
            # Hide manual auth button and hint
            if hasattr(self, 'manual_auth_btn'):
                self.manual_auth_btn.setVisible(False)
            if hasattr(self, 'manual_auth_hint'):
                self.manual_auth_hint.setVisible(False)
            # Show monitoring button
            if hasattr(self, 'start_monitor_btn'):
                self.start_monitor_btn.setVisible(True)
        except Exception as e:
            print(f"[MANUAL AUTH DONE ERROR] {str(e)}")

    def _show_auth_dialog(self):
        """Show the auth confirmation dialog as overlay within main window (must be called from main thread)"""
        try:
            # Apply blur effect to content area (not title bar)
            if hasattr(self, 'right_panel'):
                self.blur_effect = QGraphicsBlurEffect()
                self.blur_effect.setBlurRadius(5)
                self.right_panel.setGraphicsEffect(self.blur_effect)

            # Create the dialog as child of main window
            self.auth_dialog = AuthConfirmDialog(self)
            self.auth_dialog.confirmed.connect(self._on_auth_confirmed)
            self.auth_dialog.cancelled.connect(self._on_auth_cancelled)

            # Center the dialog within the main window
            dialog_x = (self.width() - self.auth_dialog.width()) // 2
            dialog_y = (self.height() - self.auth_dialog.height()) // 2
            self.auth_dialog.move(dialog_x, dialog_y)

            # Raise to top and show
            self.auth_dialog.raise_()
            self.auth_dialog.show()
        except Exception as e:
            print(f"[SHOW AUTH DIALOG ERROR] {str(e)}")

    def _hide_auth_dialog(self):
        """Hide the auth dialog and remove blur effect"""
        try:
            # Remove blur effect
            if hasattr(self, 'right_panel') and hasattr(self, 'blur_effect'):
                self.right_panel.setGraphicsEffect(None)
                self.blur_effect = None

            # Hide and delete dialog
            if hasattr(self, 'auth_dialog') and self.auth_dialog:
                self.auth_dialog.hide()
                self.auth_dialog.deleteLater()
                self.auth_dialog = None
        except Exception as e:
            print(f"[HIDE AUTH DIALOG ERROR] {str(e)}")

    def _on_auth_confirmed(self):
        """Handle auth confirmation - hide dialog, show monitoring button"""
        try:
            self._hide_auth_dialog()
            self._on_manual_auth_done()  # Show monitoring button
            self.add_log("✅ 確認が完了しました")
            self.add_log("📊 「監視開始」ボタンで自動監視を開始できます")
        except Exception as e:
            print(f"[AUTH CONFIRMED ERROR] {str(e)}")

    def _on_auth_cancelled(self):
        """Handle auth cancellation - stop the waiting thread"""
        try:
            self._hide_auth_dialog()
            self.add_log("⚠ 確認がキャンセルされました")
            self.add_log("📱 再度「スタート」ボタンを押してください")

            # Stop monitoring and reset state
            self.monitoring_active = False
            self.browser_ready_for_monitoring = False
            self.continue_to_monitoring.set()  # Unblock the waiting thread so it can exit
        except Exception as e:
            print(f"[AUTH CANCELLED ERROR] {str(e)}")

    # === Login Data Management ===
    def load_login_data(self):
        """Load saved login data from JSON file"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    login_data = data.get('login', {})

                    if login_data.get('email') and login_data.get('password'):
                        self.email_input.setText(login_data['email'])
                        self.password_input.setText(login_data['password'])

                        # Lock the fields since data is loaded
                        self.email_input.setReadOnly(True)
                        self.password_input.setReadOnly(True)
                        self.save_login_btn.setEnabled(False)

                        self.add_log("✓ 保存されたログイン情報を読み込みました")
        except Exception as e:
            self.add_log(f"⚠ ログイン情報の読み込みエラー: {str(e)}")

    def has_saved_login(self):
        """Check if login data exists in JSON file"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    login_data = data.get('login', {})
                    return bool(login_data.get('email') and login_data.get('password'))
        except Exception:
            pass
        return False

    def save_login_data(self, email, password):
        """Save login data to JSON file - CREATE/UPDATE operation"""
        try:
            # Validate inputs
            if not email or not password:
                self.add_log("⚠ メールアドレスまたはパスワードが空です")
                return False

            # Load existing data or create new
            data = {}
            if os.path.exists(self.data_file):
                try:
                    with open(self.data_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except json.JSONDecodeError:
                    self.add_log("⚠ データファイルが破損しています。新しく作成します")
                    data = {}

            # Update login data
            data['login'] = {
                'email': email,
                'password': password  # TODO: Should encrypt this in production
            }

            # Save to file
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

            return True
        except Exception as e:
            self.add_log(f"❌ ログイン情報の保存エラー: {str(e)}")
            print(f"Error saving login data: {str(e)}")
            return False

    def delete_login_from_json(self):
        """Delete login data from JSON file - DELETE operation"""
        try:
            # Load existing data
            data = {}
            if os.path.exists(self.data_file):
                try:
                    with open(self.data_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except json.JSONDecodeError:
                    self.add_log("⚠ データファイルが破損しています。新しく作成します")
                    data = {}

            # Remove login data if it exists
            if 'login' in data:
                del data['login']

            # Save to file
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

            return True
        except Exception as e:
            self.add_log(f"❌ ログイン情報の削除エラー: {str(e)}")
            print(f"Error deleting login data: {str(e)}")
            return False

    def show_sample_products(self, keyword):
        """Add a single product to the monitoring table - CREATE operation"""
        # Check if table already has 3 items (maximum limit)
        if self.product_table.rowCount() >= 3:
            self.add_log("⚠ 警告: 商品は最大3件まで登録できます")
            self.add_log("   既存の商品を削除してから追加してください")
            return

        # Check for duplicate product name
        for row in range(self.product_table.rowCount()):
            existing_item = self.product_table.item(row, 0)  # Check column 0 (商品名)
            if existing_item and existing_item.text() == keyword:
                self.add_log(f"⚠ '{keyword}' は既に監視リストに追加されています")
                return

        # Create Kitamura search URL with the keyword (replace spaces with +)
        search_url = f"https://shop.kitamura.jp/ec/list?keyword={keyword.replace(' ', '+')}&type=u"

        # Add product to table using helper method
        self.add_product_to_table(keyword, search_url)

        # Save to JSON
        self.save_products_to_json()

        self.add_log(f"✓ '{keyword}' を監視リストに追加しました")

    def delete_product_row(self, button=None):
        """Delete a product row from the table - DELETE operation"""
        try:
            # Find which row this button is in
            if button is None:
                self.add_log("⚠ 削除ボタンが見つかりません")
                return

            row = -1
            for r in range(self.product_table.rowCount()):
                # Delete button is in column 4 (削除 column)
                if self.product_table.cellWidget(r, 4) == button:
                    row = r
                    break

            if row == -1:
                self.add_log("⚠ 削除する行が見つかりません")
                return

            # Get product name before deleting (column 0)
            product_item = self.product_table.item(row, 0)
            product_name = product_item.text() if product_item else "商品"

            # If we're editing this row, clear the form and reset editing state
            if self.editing_row == row:
                self.search_input.clear()
                self.min_price_input.clear()
                self.max_price_input.clear()
                self.editing_row = None

            # Remove the row
            self.product_table.removeRow(row)

            # If we were editing a row after this one, adjust the editing_row index
            if self.editing_row is not None and self.editing_row > row:
                self.editing_row -= 1

            # Save updated product list to JSON
            if self.save_products_to_json():
                self.add_log(f"🗑️ '{product_name}' を削除しました (JSONに保存済み)")
            else:
                self.add_log(f"🗑️ '{product_name}' を削除しました (⚠ JSON保存失敗)")

        except Exception as e:
            self.add_log(f"❌ 削除エラー: {str(e)}")
            import traceback
            traceback.print_exc()

    def on_clear_all_products(self):
        """Clear all products from table - DELETE ALL operation"""
        product_count = self.product_table.rowCount()

        if product_count == 0:
            self.add_log("⚠ 削除する商品がありません")
            return

        # Clear all rows
        self.product_table.setRowCount(0)

        # Save empty product list to JSON
        self.save_products_to_json()

        self.add_log(f"🗑️ 全ての商品を削除しました ({product_count}件)")

    # === Speed Test Functions ===
    def on_speed_test_start(self):
        """Handle speed test start button click"""
        # Validate URL
        test_url = self.speed_test_url_input.text().strip()
        if not test_url:
            self.add_log("⚠ テストURLを入力してください")
            return

        if "shop.kitamura.jp" not in test_url:
            self.add_log("⚠ 正しいショップURLを入力してください")
            return

        # Check if browser is available
        if not BROWSER_AVAILABLE:
            self.add_log("❌ ブラウザライブラリがインストールされていません")
            return

        # Check if browser is running
        if self.shop_page is None:
            self.add_log("❌ 先に「スタート」ボタンでブラウザを起動してください")
            return

        # Note: Speed tests can now run during monitoring (monitoring will pause automatically)

        # Check if browser is ready and waiting
        if not self.browser_ready_for_monitoring:
            self.add_log("⚠ ブラウザが待機状態ではありません")
            self.add_log("   「スタート」ボタンを押してブラウザを起動してください")
            return

        # Check if speed test is already running
        if self.speed_test_running:
            self.add_log("⚠ スピードテストは既に実行中です")
            return

        # Queue the speed test task to run in browser thread
        self.add_log("🚀 スピードテスト開始")
        self.add_log(f"📄 URL: {test_url}")
        self.add_log("⏳ ブラウザスレッドでタスクを実行中...")

        self.browser_task_queue.put({
            'type': 'speed_test',
            'url': test_url
        })

    def on_speed_test_clear(self):
        """Clear speed test results"""
        self.speed_test_results = []
        self._update_timer_safe("0.000秒")
        self.add_log("🗑️ テスト結果をクリアしました")

    def _execute_speed_test_task(self, test_url):
        """Execute speed test in the browser thread (called while waiting for monitoring)"""
        try:
            self.speed_test_running = True

            # Check if browser is available
            if self.shop_page is None:
                self.add_log("❌ ブラウザが利用できません")
                return

            results = []

            # Run single speed test
            self.add_log(f">")
            self.add_log(f"📊 テスト実行中...")
            self.add_log(f">")

            # Use the existing page directly (we're in the browser thread)
            result = self._run_single_speed_test_with_page(self.shop_page, test_url, 1)

            if result:
                self.add_log(f"✓ テスト購入完了")
            else:
                self.add_log(f"❌ テスト失敗")

        except Exception as e:
            self.add_log(f"❌ スピードテストエラー: {str(e)}")
            import traceback
            self.add_log(f"詳細: {traceback.format_exc()}")
        finally:
            self.speed_test_running = False

    def clear_cart(self, page):
        """Clear all items from shopping cart"""
        try:
            # Go to cart page
            page.goto("https://shop.kitamura.jp/ec/cart", wait_until="domcontentloaded")
            time.sleep(0.3)

            # Find and click all delete buttons
            max_attempts = 10
            for attempt in range(max_attempts):
                try:
                    # Find delete buttons (trash icon buttons)
                    delete_buttons = page.locator("//button[contains(@class,'cart-item-delete-btn') or .//i[contains(@class,'fa-trash')]]").all()

                    if not delete_buttons or len(delete_buttons) == 0:
                        # No more items to delete
                        break

                    # Click first delete button
                    delete_buttons[0].scroll_into_view_if_needed()
                    delete_buttons[0].click()
                    time.sleep(0.2)

                except Exception:
                    break

            self.add_log("✓ カートをクリアしました")

        except Exception as e:
            self.add_log(f"⚠ カートクリアエラー: {str(e)}")

    def display_speed_test_summary(self, results):
        """Display summary statistics for speed test results"""
        try:
            self.add_log(">")
            self.add_log("📊 スピードテスト結果")
            self.add_log(">")

            if not results:
                self.add_log("⚠ 結果がありません")
                return

            # Calculate statistics
            total_times = [r['total_time'] for r in results]
            avg_time = sum(total_times) / len(total_times)
            min_time = min(total_times)
            max_time = max(total_times)

            # Calculate standard deviation
            variance = sum((t - avg_time) ** 2 for t in total_times) / len(total_times)
            std_dev = variance ** 0.5

            self.add_log(f"テスト回数: {len(results)}")
            self.add_log(f"平均時間: {avg_time:.3f}秒")
            self.add_log(f"最速時間: {min_time:.3f}秒")
            self.add_log(f"最遅時間: {max_time:.3f}秒")
            self.add_log(f"標準偏差: {std_dev:.3f}秒")
            self.add_log("")

            # Display individual results
            self.add_log("個別結果:")
            for r in results:
                self.add_log(f"  テスト{r['test_num']}: {r['total_time']:.3f}秒")

            # Display step-by-step timing breakdown (average)
            if results and 'timings' in results[0]:
                self.add_log("")
                self.add_log("ステップ別平均時間:")

                step_names = {
                    'page_load': 'ページ読み込み',
                    'add_to_cart': 'カート追加',
                    'checkout_step1': 'チェックアウト1',
                    'checkout_step2': 'チェックアウト2',
                    'checkbox': 'チェックボックス',
                    'order_confirm': '注文確認',
                    'final_page_load': '最終ページ読み込み'
                }

                for step_key, step_name in step_names.items():
                    step_times = [r['timings'].get(step_key, 0) for r in results if 'timings' in r]
                    if step_times:
                        avg_step_time = sum(step_times) / len(step_times)
                        self.add_log(f"  {step_name}: {avg_step_time:.3f}秒")

            # Display precise detection times (microsecond precision)
            if results and results[0].get('detection_times'):
                self.add_log("")
                self.add_log("🎯 ボタン検出時間 (ミリ秒精度):")

                detection_step_names = {
                    'add_to_cart': 'カートボタン検出',
                    'checkout_step1': 'チェックアウト1検出',
                    'checkout_step2': 'チェックアウト2検出',
                    'order_confirm': '注文確認ボタン検出'
                }

                for step_key, step_name in detection_step_names.items():
                    detection_times = [r.get('detection_times', {}).get(step_key, 0) for r in results]
                    detection_times = [t for t in detection_times if t > 0]  # Filter out zeros
                    if detection_times:
                        avg_detection_time = sum(detection_times) / len(detection_times)
                        min_detection_time = min(detection_times)
                        max_detection_time = max(detection_times)
                        self.add_log(f"  {step_name}:")
                        self.add_log(f"    平均: {avg_detection_time:.1f}ms | 最速: {min_detection_time:.1f}ms | 最遅: {max_detection_time:.1f}ms")

            self.add_log(">")

        except Exception as e:
            self.add_log(f"❌ 統計表示エラー: {str(e)}")

    def find_system_browser(self):
        """Find system-installed Chrome or Edge browser"""
        # Common Chrome installation paths
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        ]

        # Common Edge installation paths
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
        ]

        # Try Chrome first
        for path in chrome_paths:
            if os.path.exists(path):
                self.add_log(f"✓ Google Chromeを検出: {path}")
                return path

        # Try Edge as fallback
        for path in edge_paths:
            if os.path.exists(path):
                self.add_log(f"✓ Microsoft Edgeを検出: {path}")
                return path

        return None

    def _run_detection_browser_async(self):
        """Helper method to run async browser startup in a separate thread"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self._start_detection_browser())
            self.detection_loop = loop
            return result
        except Exception as e:
            loop.close()
            raise e

    async def _create_single_detection_browser(self, browser_name="検知ブラウザ"):
        """Create a single detection browser in guest mode and return browser object"""
        try:
            self.add_log(f"🔍 {browser_name}を起動中（ゲストモード）...")

            # Start Playwright for detection browser (async)
            playwright = await async_playwright().start()

            # Launch browser WITHOUT persistent context (guest mode)
            launch_kwargs = {
                "headless": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            browser_started = False
            browser = None

            try:
                # Use launch() instead of launch_persistent_context() for guest mode
                browser = await playwright.chromium.launch(**launch_kwargs)
                browser_started = True
                self.add_log(f"✓ {browser_name}: Playwright Chromiumを使用します")
            except Exception as e_primary:
                self.add_log(f"⚠ {browser_name}: PlaywrightのChromiumが見つかりません。既存ブラウザで再試行します...")

                # Try Microsoft Edge channel
                if not browser_started:
                    try:
                        browser = await playwright.chromium.launch(
                            channel="msedge", **launch_kwargs
                        )
                        browser_started = True
                        self.add_log(f"✓ {browser_name}: Microsoft Edge を使用して起動しました")
                    except Exception:
                        pass

                # Try Google Chrome channel
                if not browser_started:
                    try:
                        browser = await playwright.chromium.launch(
                            channel="chrome", **launch_kwargs
                        )
                        browser_started = True
                        self.add_log(f"✓ {browser_name}: Google Chrome を使用して起動しました")
                    except Exception as e_fallback:
                        self.add_log(f"❌ {browser_name}起動に失敗しました: {str(e_fallback)}")
                        try:
                            if playwright:
                                await playwright.stop()
                        finally:
                            playwright = None
                        return None

            # Create new context (incognito/guest mode)
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(60000)

            self.add_log(f"✓ {browser_name}を起動しました（ゲストモード）")
            
            # Return browser object: [playwright, browser, context, page]
            return {
                'playwright': playwright,
                'browser': browser,
                'context': context,
                'page': page,
                'name': browser_name
            }

        except Exception as e:
            self.add_log(f"❌ {browser_name}起動エラー: {str(e)}")
            import traceback
            self.add_log(f"詳細: {traceback.format_exc()}")
            return None

    async def _start_multiple_detection_browsers(self, urls_and_names):
        """Start multiple detection browsers in parallel
        Args:
            urls_and_names: List of tuples [(url, name, product_filter), ...]
        Returns:
            List of browser objects or None if failed
        """
        try:
            self.add_log(f"🔍 {len(urls_and_names)}個の検知ブラウザを起動中...")
            
            # Create all browsers in parallel
            tasks = []
            for url, name, product_filter in urls_and_names:
                task = self._create_single_detection_browser(name)
                tasks.append((task, url, name, product_filter))
            
            # Wait for all browsers to start
            browser_objects = []
            for task, url, name, product_filter in tasks:
                browser_obj = await task
                if browser_obj:
                    browser_obj['url'] = url
                    browser_obj['product_filter'] = product_filter
                    browser_objects.append(browser_obj)
                else:
                    self.add_log(f"❌ {name}の起動に失敗しました")
            
            if browser_objects:
                self.detection_browsers = browser_objects
                # For backward compatibility, set first browser as default
                if browser_objects:
                    first = browser_objects[0]
                    self.detection_playwright = first['playwright']
                    self.detection_browser = first['browser']
                    self.detection_context = first['context']
                    self.detection_page = first['page']
                self.add_log(f"✓ {len(browser_objects)}個の検知ブラウザを起動しました")
                return True
            else:
                self.add_log("❌ 検知ブラウザの起動に失敗しました")
                return False
                
        except Exception as e:
            self.add_log(f"❌ 複数検知ブラウザ起動エラー: {str(e)}")
            import traceback
            self.add_log(f"詳細: {traceback.format_exc()}")
            return False

    async def _start_detection_browser(self):
        """Start detection browsers (legacy compatibility - creates single browser)"""
        # For backward compatibility, create one browser and store it
        browser_obj = await self._create_single_detection_browser("検知ブラウザ")
        if browser_obj:
            # Store in old format for compatibility
            self.detection_playwright = browser_obj['playwright']
            self.detection_browser = browser_obj['browser']
            self.detection_context = browser_obj['context']
            self.detection_page = browser_obj['page']
            # Also add to browsers list
            self.detection_browsers = [browser_obj]
            return True
        return False

    def _close_detection_browser(self):
        """Close all detection browsers"""
        try:
            # Close all browsers in the list
            for browser_obj in self.detection_browsers:
                try:
                    if browser_obj.get('context'):
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            loop.run_until_complete(browser_obj['context'].close())
                        except Exception:
                            pass
                        finally:
                            loop.close()
                    if browser_obj.get('browser'):
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            loop.run_until_complete(browser_obj['browser'].close())
                        except Exception:
                            pass
                        finally:
                            loop.close()
                    if browser_obj.get('playwright'):
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            loop.run_until_complete(browser_obj['playwright'].stop())
                        except Exception:
                            pass
                        finally:
                            loop.close()
                except Exception as e:
                    self.add_log(f"⚠ 検知ブラウザ終了時の警告: {str(e)}")
            
            # Clear the list
            self.detection_browsers = []
            
            # Also close legacy single browser (for backward compatibility)
            if self.detection_context:
                if self.detection_loop is not None:
                    try:
                        self.detection_loop.run_until_complete(self.detection_context.close())
                    except Exception:
                        pass
                else:
                    try:
                        self.detection_context.close()
                    except Exception:
                        pass
        except Exception as e:
            self.add_log(f"⚠ 検知コンテキスト終了時の警告: {str(e)}")
        finally:
            self.detection_context = None
            self.detection_page = None

        try:
            if self.detection_browser:
                if self.detection_loop is not None:
                    try:
                        self.detection_loop.run_until_complete(self.detection_browser.close())
                    except Exception:
                        pass
                else:
                    self.detection_browser.close()
        except Exception as e:
            self.add_log(f"⚠ 検知ブラウザ終了時の警告: {str(e)}")
        finally:
            self.detection_browser = None

        try:
            if self.detection_playwright:
                if self.detection_loop is not None:
                    try:
                        self.detection_loop.run_until_complete(self.detection_playwright.stop())
                    except Exception:
                        pass
                else:
                    if hasattr(self.detection_playwright, 'stop'):
                        self.detection_playwright.stop()
        except Exception as e:
            self.add_log(f"⚠ 検知Playwright停止時の警告: {str(e)}")
        finally:
            self.detection_playwright = None
            self.detection_loop = None

    def _close_playwright_session(self, log_message=None):
        """Close any running Playwright browser/context and release the persistent profile lock."""
        if log_message:
            self.add_log(log_message)

        try:
            if self.shop_browser:
                self.shop_browser.close()
        except Exception as e:
            self.add_log(f"⚠ ブラウザ終了時の警告: {str(e)}")
        finally:
            self.shop_browser = None
            self.shop_context = None
            self.shop_page = None

        try:
            if self.playwright:
                self.playwright.stop()
        except Exception as e:
            self.add_log(f"⚠ Playwright停止時の警告: {str(e)}")
        finally:
            self.playwright = None



    def stop_monitoring(self):
        """Stop monitoring (keeps browser open)"""
        try:
            # Just stop the monitoring loop
            self.monitoring_active = False

            # Reset buttons
            self.start_monitor_btn.setVisible(True)
            self.stop_monitor_btn.setVisible(False)

            # Log the action
            self.add_log("🛑 監視を停止しました")
            self.add_log("ℹ️ ブラウザは開いたままです")

        except Exception as e:
            # Prevent app crash on any error
            self.add_log(f"❌ 停止処理中にエラー: {str(e)}")
            import traceback
            print(f"Stop monitoring error: {traceback.format_exc()}")
            # Ensure buttons are reset even on error
            try:
                self.start_monitor_btn.setVisible(True)
                self.stop_monitor_btn.setVisible(False)
            except:
                pass

    # === Product JSON Management ===
    def save_products_to_json(self):
        """Save all products from table to JSON file - CREATE/UPDATE/DELETE sync"""
        try:
            # Validate table exists
            if not hasattr(self, 'product_table') or self.product_table is None:
                return False

            # Load existing data or create new
            data = {}
            if os.path.exists(self.data_file):
                try:
                    with open(self.data_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except json.JSONDecodeError:
                    self.add_log("⚠ データファイルが破損しています。新しく作成します")
                    data = {}

            # Collect all products from table (reflects CREATE/DELETE operations)
            products = []
            for row in range(self.product_table.rowCount()):
                try:
                    name_item = self.product_table.item(row, 0)
                    min_price_item = self.product_table.item(row, 1)
                    max_price_item = self.product_table.item(row, 2)

                    if name_item:
                        name = name_item.text()

                        if name:
                            # Get prices from item data
                            min_price = min_price_item.data(Qt.UserRole) if min_price_item else None
                            max_price = max_price_item.data(Qt.UserRole) if max_price_item else None

                            products.append({
                                'name': name,
                                'min_price': min_price,
                                'max_price': max_price
                            })
                except Exception as row_error:
                    print(f"Error reading row {row}: {str(row_error)}")
                    continue

            # Update products data (sync with JSON)
            data['products'] = products

            # Save to file (persist changes immediately)
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

            return True
        except Exception as e:
            self.add_log(f"❌ 商品データの保存エラー: {str(e)}")
            print(f"Error saving products: {str(e)}")
            return False

    def load_products_from_json(self):
        """Load products from JSON file and populate table - READ operation"""
        try:
            # Validate table exists
            if not hasattr(self, 'product_table') or self.product_table is None:
                return

            if os.path.exists(self.data_file):
                try:
                    with open(self.data_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except json.JSONDecodeError:
                    self.add_log("⚠ データファイルが破損しています")
                    return

                products = data.get('products', [])

                # Clear existing table rows
                try:
                    self.product_table.setRowCount(0)
                except Exception as clear_error:
                    self.add_log(f"⚠ テーブルのクリアエラー: {str(clear_error)}")
                    return

                # Add each product to the table
                loaded_count = 0
                for idx, product in enumerate(products):
                    try:
                        if not isinstance(product, dict):
                            continue

                        name = product.get('name', '')
                        min_price = product.get('min_price')
                        max_price = product.get('max_price')

                        if name:
                            self.add_product_to_table(name, min_price, max_price)
                            loaded_count += 1
                    except Exception as product_error:
                        print(f"Error loading product {idx}: {str(product_error)}")
                        continue

                if loaded_count > 0:
                    self.add_log(f"✓ {loaded_count}件の商品を読み込みました")

        except Exception as e:
            self.add_log(f"⚠ 商品データの読み込みエラー: {str(e)}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")

    def add_product_to_table(self, name, min_price=None, max_price=None):
        """Add a product to the table - display name and prices (no URL storage)"""
        try:
            # Validate inputs
            if not name:
                return

            # Validate table exists
            if not hasattr(self, 'product_table') or self.product_table is None:
                return

            row_position = self.product_table.rowCount()
            self.product_table.insertRow(row_position)

            # Product name (no URL needed)
            product_name = QTableWidgetItem(str(name))
            product_name.setFlags(product_name.flags() & ~Qt.ItemIsEditable)
            product_name.setTextAlignment(Qt.AlignCenter)
            product_name.setToolTip(str(name))  # Show full name on hover
            self.product_table.setItem(row_position, 0, product_name)

            # Min price - display as static text
            min_price_text = f"{min_price:,}¥" if min_price is not None else "-"
            min_price_item = QTableWidgetItem(min_price_text)
            min_price_item.setFlags(min_price_item.flags() & ~Qt.ItemIsEditable)
            min_price_item.setTextAlignment(Qt.AlignCenter)
            min_price_item.setData(Qt.UserRole, min_price)  # Store raw value
            self.product_table.setItem(row_position, 1, min_price_item)

            # Max price - display as static text
            max_price_text = f"{max_price:,}¥" if max_price is not None else "-"
            max_price_item = QTableWidgetItem(max_price_text)
            max_price_item.setFlags(max_price_item.flags() & ~Qt.ItemIsEditable)
            max_price_item.setTextAlignment(Qt.AlignCenter)
            max_price_item.setData(Qt.UserRole, max_price)  # Store raw value
            self.product_table.setItem(row_position, 2, max_price_item)

            # Add change button - blue/green with consistent font size
            change_btn = QPushButton("✏️")
            change_btn.setStyleSheet("""
                QPushButton {
                    background-color: #2196F3;
                    color: white;
                    border: none;
                    border-radius: 3px;
                    padding: 6px 12px;
                    font-size: 16px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #0b7dda;
                }
                QPushButton:pressed {
                    background-color: #0a6bc4;
                }
            """)
            change_btn.clicked.connect(lambda checked=False, row=row_position: self.on_change_product(row))
            self.product_table.setCellWidget(row_position, 3, change_btn)

            # Add delete button - red with consistent font size
            delete_btn = QPushButton("×")
            delete_btn.setStyleSheet("""
                QPushButton {
                    background-color: #f44336;
                    color: white;
                    border: none;
                    border-radius: 3px;
                    padding: 6px 12px;
                    font-size: 16px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #da190b;
                }
                QPushButton:pressed {
                    background-color: #c41409;
                }
            """)

            # Use a closure that captures the button properly
            delete_btn.clicked.connect(lambda checked=False, btn=delete_btn: self.delete_product_row(btn))
            self.product_table.setCellWidget(row_position, 4, delete_btn)

        except Exception as e:
            self.add_log(f"❌ 商品の追加エラー: {str(e)}")
            print(f"Error adding product to table: {str(e)}")

    def update_product_row(self, row, name, min_price=None, max_price=None):
        """Update an existing product row in the table"""
        try:
            # Validate table exists and row is valid
            if not hasattr(self, 'product_table') or self.product_table is None:
                return
            if row < 0 or row >= self.product_table.rowCount():
                return

            # Update product name (no URL needed)
            product_name = QTableWidgetItem(str(name))
            product_name.setFlags(product_name.flags() & ~Qt.ItemIsEditable)
            product_name.setTextAlignment(Qt.AlignCenter)
            product_name.setToolTip(str(name))
            self.product_table.setItem(row, 0, product_name)

            # Update min price
            min_price_text = f"{min_price:,}¥" if min_price is not None else "-"
            min_price_item = QTableWidgetItem(min_price_text)
            min_price_item.setFlags(min_price_item.flags() & ~Qt.ItemIsEditable)
            min_price_item.setTextAlignment(Qt.AlignCenter)
            min_price_item.setData(Qt.UserRole, min_price)
            self.product_table.setItem(row, 1, min_price_item)

            # Update max price
            max_price_text = f"{max_price:,}¥" if max_price is not None else "-"
            max_price_item = QTableWidgetItem(max_price_text)
            max_price_item.setFlags(max_price_item.flags() & ~Qt.ItemIsEditable)
            max_price_item.setTextAlignment(Qt.AlignCenter)
            max_price_item.setData(Qt.UserRole, max_price)
            self.product_table.setItem(row, 2, max_price_item)

            # Buttons remain the same (no need to recreate them)

        except Exception as e:
            self.add_log(f"❌ 商品の更新エラー: {str(e)}")
            print(f"Error updating product row: {str(e)}")

    # === Stylish Button Factory ===
    def create_header_button(self, text, base_color, border_color, hover_glow="#FFFFFF"):
        btn = QPushButton(text)
        btn.setFixedSize(32, 32)
        btn.setFont(QFont("Segoe UI", 12, QFont.Bold))
        btn.setStyleSheet(f"""
            QPushButton {{
                color: white;
                border: 2px solid {border_color};
                border-radius: 15px;
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 {border_color},
                    stop:1 #6B7A76
                );
            }}
            QPushButton:hover {{
                background-color: {hover_glow};
                border: 2px solid white;
            }}
            QPushButton:pressed {{
                background-color: rgba(255,255,255,0.3);
            }}
        """)
        return btn

    # === Painting Border + Header ===
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # --- Gradient border with 0.5 opacity ---
        grad = QLinearGradient(0, 0, self.width(), self.height())
        color_start = QColor("#AA875F")
        color_end = QColor("#6B7A76")
        color_start.setAlphaF(0.5)
        color_end.setAlphaF(0.5)
        grad.setColorAt(0, color_start)
        grad.setColorAt(1, color_end)

        pen = QPen(QBrush(grad), self.border_thickness)
        painter.setPen(pen)
        painter.setBrush(Qt.transparent)

        rect = QRectF(
            self.border_thickness / 2,
            self.border_thickness / 2,
            self.width() - self.border_thickness,
            self.height() - self.border_thickness
        )
        painter.drawRoundedRect(rect, 0, 0)

        # --- Gradient header bar with 0.8 opacity ---
        header_grad = QLinearGradient(0, 0, self.width(), 0)
        header_color_start = QColor("#AA875F")
        header_color_end = QColor("#6B7A76")
        header_color_start.setAlphaF(0.8)
        header_color_end.setAlphaF(0.8)
        header_grad.setColorAt(0, header_color_start)
        header_grad.setColorAt(1, header_color_end)

        painter.fillRect(
            0,
            0,
            self.width(),
            self.header_height + self.border_thickness,
            QBrush(header_grad)
        )

    # === Window Dragging ===
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.pos().y() <= self.header_height:
            # Disable dragging when maximized
            if not self.is_maximized:
                self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()
                event.accept()

    def mouseMoveEvent(self, event):
        # Only allow moving when not maximized
        if self.drag_pos and event.buttons() == Qt.LeftButton and not self.is_maximized:
            self.move(event.globalPos() - self.drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None

    # === Maximize / Restore ===
    def toggle_max_restore(self):
        if self.is_maximized:
            self.showNormal()
            self.is_maximized = False
        else:
            self.showMaximized()
            self.is_maximized = True

    # === Cleanup ===
    def closeEvent(self, event):
        """Handle window close event - properly cleanup Playwright resources"""
        try:
            self.monitoring_active = False
            print("Application closing...")

            # Wait for threads to finish
            if self.detection_thread and self.detection_thread.is_alive():
                print("Waiting for detection thread...")
                self.detection_thread.join(timeout=2.0)
            
            if self.purchase_thread and self.purchase_thread.is_alive():
                print("Waiting for purchase thread...")
                self.purchase_thread.join(timeout=2.0)

            if self.monitor_thread and self.monitor_thread.is_alive():
                print("Waiting for monitor thread...")
                self.monitor_thread.join(timeout=2.0)

            # Close detection browser
            try:
                if self.detection_browser:
                    print("Closing detection browser...")
                    self._close_detection_browser()
            except Exception as e:
                print(f"Error closing detection browser: {str(e)}")

            # Close purchase browser
            try:
                if self.shop_browser:
                    print("Closing Playwright browser...")
                    self.shop_browser.close()
                    self.shop_browser = None
            except Exception as e:
                print(f"Error closing browser: {str(e)}")

            try:
                if self.playwright:
                    print("Stopping Playwright...")
                    self.playwright.stop()
                    self.playwright = None
            except Exception as e:
                print(f"Error stopping Playwright: {str(e)}")

            print("Cleanup complete")

        except Exception as e:
            print(f"Error during close: {str(e)}")
        finally:
            event.accept()

    # === Always On Top Toggle ===
    def toggle_always_on_top(self):
        """Toggle the always-on-top state"""
        try:
            self.always_on_top = not self.always_on_top

            # Update window flags
            if self.always_on_top:
                self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
                self.btn_pin.setStyleSheet("""
                    QPushButton {
                        color: white;
                        border: 2px solid #AA875F;
                        border-radius: 15px;
                        background: qlineargradient(
                            x1:0, y1:0, x2:1, y2:1,
                            stop:0 #4CAF50,
                            stop:1 #45a049
                        );
                    }
                    QPushButton:hover {
                        background-color: #fac069;
                        border: 2px solid white;
                    }
                    QPushButton:pressed {
                        background-color: rgba(255,255,255,0.3);
                    }
                """)
                self.add_log("📌 常に手前に表示: ON")
            else:
                self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)
                self.btn_pin.setStyleSheet("""
                    QPushButton {
                        color: white;
                        border: 2px solid #AA875F;
                        border-radius: 15px;
                        background: qlineargradient(
                            x1:0, y1:0, x2:1, y2:1,
                            stop:0 #AA875F,
                            stop:1 #6B7A76
                        );
                    }
                    QPushButton:hover {
                        background-color: #fac069;
                        border: 2px solid white;
                    }
                    QPushButton:pressed {
                        background-color: rgba(255,255,255,0.3);
                    }
                """)
                self.add_log("📌 常に手前に表示: OFF")

            # Need to show the window again after changing flags
            self.show()
        except Exception as e:
            print(f"Error toggling always on top: {str(e)}")
            self.add_log("⚠ ウィンドウ設定の変更に失敗しました")

    def toggle_log_panel(self):
        """Toggle between log panel full-screen and main panel full-screen"""
        try:
            # Check current state
            log_is_visible = self.log_panel.isVisible()

            if log_is_visible:
                # Currently showing log, switch to main window
                self.log_panel.setVisible(False)
                self.right_panel.setVisible(True)

                # Update button to inactive state (gray)
                self.btn_log_toggle.setStyleSheet("""
                    QPushButton {
                        color: white;
                        border: 2px solid #AA875F;
                        border-radius: 15px;
                        background: qlineargradient(
                            x1:0, y1:0, x2:1, y2:1,
                            stop:0 #AA875F,
                            stop:1 #6B7A76
                        );
                    }
                    QPushButton:hover {
                        background-color: #fac069;
                        border: 2px solid white;
                    }
                    QPushButton:pressed {
                        background-color: rgba(255,255,255,0.3);
                    }
                """)
            else:
                # Currently showing main window, switch to log
                self.log_panel.setVisible(True)
                self.right_panel.setVisible(False)

                # Update button to active state (green)
                self.btn_log_toggle.setStyleSheet("""
                    QPushButton {
                        color: white;
                        border: 2px solid #AA875F;
                        border-radius: 15px;
                        background: qlineargradient(
                            x1:0, y1:0, x2:1, y2:1,
                            stop:0 #4CAF50,
                            stop:1 #45a049
                        );
                    }
                    QPushButton:hover {
                        background-color: #fac069;
                        border: 2px solid white;
                    }
                    QPushButton:pressed {
                        background-color: rgba(255,255,255,0.3);
                    }
                """)
                self.add_log("📋 ログ画面に切り替えました")

        except Exception as e:
            print(f"Error toggling log panel: {str(e)}")

    # === Product Link Extraction (Fast Version) ===
    def extract_product_links_by_filter_fast(self, page, product_filters, loop=None):
        """Fast product link extraction - minimal logging for maximum speed

        Args:
            page: Playwright page object
            product_filters: List of dicts with 'name', 'min_price', 'max_price'
            loop: Optional event loop for async operations (if None, uses self.detection_loop or sync)
        """
        try:
            # Wait briefly for products to render
            try:
                # Use provided loop, or fall back to self.detection_loop, or use sync
                if loop is not None:
                    loop.run_until_complete(
                        page.wait_for_selector('.product-area', timeout=1500)
                    )
                elif self.detection_loop is not None:
                    self.detection_loop.run_until_complete(
                        page.wait_for_selector('.product-area', timeout=1500)
                    )
                else:
                    page.wait_for_selector('.product-area', timeout=1500)
            except Exception:
                return []

            filter_payload = []
            for f in product_filters:
                name = (f.get('name') or '').strip().lower()
                if not name:
                    continue
                filter_payload.append({
                    'name': name,
                    'min': f.get('min_price'),
                    'max': f.get('max_price'),
                })

            if not filter_payload:
                return []

            # Evaluate JavaScript - handle both sync and async
            evaluate_script = """
                (filters) => {
                    const matches = [];
                    const seen = new Set();
                    const links = document.querySelectorAll('.product-l-area a.product-link');

                    for (const link of links) {
                        try {
                            const href = link.href;
                            if (!href || seen.has(href)) {
                                continue;
                            }

                            const nameNode = link.querySelector('.product-name');
                            const productName = nameNode ? nameNode.textContent.trim().toLowerCase() : '';
                            if (!productName) {
                                continue;
                            }

                            const area = link.closest('.product-area');
                            if (!area) {
                                continue;
                            }

                            const cover = area.querySelector('.product-img-cover');
                            if (cover) {
                                const coverText = (cover.textContent || '').toLowerCase();
                                const coverClass = (cover.className || '').toLowerCase();
                                if (coverText.includes('sold') || coverText.includes('coming') ||
                                    coverClass.includes('sold') || coverClass.includes('coming')) {
                                    continue;
                                }
                            }

                            let price = null;
                            const priceNode = link.querySelector('span.product-price');
                            if (priceNode) {
                                const digits = (priceNode.textContent || '').replace(/[^0-9]/g, '');
                                if (digits) {
                                    price = parseInt(digits, 10);
                                }
                            }

                            let matched = false;
                            for (const filter of filters) {
                                if (!filter.name || !productName.includes(filter.name)) {
                                    continue;
                                }

                                if (filter.min != null && (price == null || price < filter.min)) {
                                    continue;
                                }
                                if (filter.max != null && (price == null || price > filter.max)) {
                                    continue;
                                }

                                matched = true;
                                break;
                            }

                            if (matched) {
                                seen.add(href);
                                matches.push(href);
                            }
                        } catch (error) {
                            continue;
                        }
                    }

                    return matches;
                }
            """
            
            if self.detection_loop is not None:
                matched_links = self.detection_loop.run_until_complete(
                    page.evaluate(evaluate_script, filter_payload)
                )
            else:
                matched_links = page.evaluate(evaluate_script, filter_payload)

            return matched_links or []

        except Exception:
            return []

    def process_product_detail_page(self, page, product_url, product_index, total_products):
        """Run the actual purchase flow using the shared ultra-fast routine."""
        try:
            result = self._run_single_speed_test_with_page(page, product_url, product_index)
            return bool(result and result.get('success'))
        except Exception as e:
            try:
                self.add_log(f"? ?????: {str(e)}")
            except Exception:
                pass
            return False

if __name__ == "__main__":
    try:
        # Enable high DPI scaling BEFORE creating QApplication
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

        app = QApplication(sys.argv)

        # Set global font with better rendering
        app_font = QFont("Segoe UI", 9)
        app_font.setStyleHint(QFont.SansSerif)
        app_font.setHintingPreference(QFont.PreferFullHinting)
        app.setFont(app_font)

        # Set up global exception handler
        def handle_exception(exc_type, exc_value, exc_traceback):
            """Global exception handler to prevent crashes"""
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_traceback)
                return

            print(">")
            print("UNHANDLED EXCEPTION - Application will continue running")
            print(">")
            import traceback
            traceback.print_exception(exc_type, exc_value, exc_traceback)
            print(">")

        sys.excepthook = handle_exception

        window = CustomWindow()
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        print(f"FATAL ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
