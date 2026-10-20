import threading
import time
import os
from kivy.app import App
from kivy.uix.widget import Widget
from kivy.clock import Clock
from jnius import autoclass

# Импортируем наш Flask-сервер из coach_web.py
# Важно: файл должен называться coach_web.py, как у нас
try:
    from coach_web import app as flask_app
except Exception as e:
    print("Ошибка импорта Flask:", e)

class WebViewWrapper(Widget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.start_server()
        # Ждем 2 секунды, чтобы сервер успел подняться, и открываем WebView
        Clock.schedule_once(self.show_webview, 2)

    def start_server(self):
        def run():
            try:
                flask_app.run(host='127.0.0.1', port=5000, threaded=True)
            except Exception as e:
                print("Ошибка запуска сервера:", e)
        threading.Thread(target=run, daemon=True).start()

    def show_webview(self, dt):
        from android.runnable import run_on_ui_thread
        
        @run_on_ui_thread
        def _show():
            try:
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity
                WebView = autoclass('android.webkit.WebView')
                WebViewClient = autoclass('android.webkit.WebViewClient')
                
                webview = WebView(activity)
                # Включаем поддержку JavaScript
                webview.getSettings().setJavaScriptEnabled(True)
                # Включаем доступ к локальному хранилищу (для сохранений)
                webview.getSettings().setDomStorageEnabled(True)
                
                webview.setWebViewClient(WebViewClient())
                webview.loadUrl("http://127.0.0.1:5000")
                
                # Делаем WebView главным экраном приложения
                activity.setContentView(webview)
            except Exception as e:
                print("Ошибка WebView:", e)
                
        _show()

class ChessTrainerApp(App):
    def build(self):
        self.title = "Шахматный Тренер"
        return WebViewWrapper()

if __name__ == '__main__':
    ChessTrainerApp().run()
