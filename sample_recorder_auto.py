import aioesphomeapi
import asyncio
import wave
from datetime import datetime
import logging
from aiohttp import web
import threading
import os
import json

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Директория для сохранения записей
RECORDINGS_DIR = "esp32_recordings"
os.makedirs(RECORDINGS_DIR, exist_ok=True)


class ESPHomeVoiceAssistant:
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password
        self.cli = None
        self.is_connected = False
        self.conversation_id = None

        # Для управления записью сегментов
        self.is_listening = False  # Постоянно слушаем ESP32
        self.is_recording_segment = False  # Записываем сегмент
        self.segment_buffer = bytearray()
        self.segment_start_time = None
        self.segment_duration = 5  # 5 секунд
        self.current_segment_filename = None
        self.audio_lock = asyncio.Lock()

        # Веб-интерфейс
        self.app = None
        self.runner = None
        self.site = None

    async def connect(self):
        """Подключение к устройству"""
        self.cli = aioesphomeapi.APIClient(self.host, self.port, self.password)

        try:
            await self.cli.connect(login=True)
            self.is_connected = True
            print("✅ Успешно подключено к устройству")

            device_info = await self.cli.device_info()
            print(f"Device: {device_info.name}, Version: {device_info.esphome_version}")
            return True

        except Exception as e:
            print(f"❌ Ошибка подключения: {e}")
            self.is_connected = False
            return False

    def start_voice_assistant(self):
        """Запуск голосового ассистента для постоянного прослушивания"""
        print("\n🎤 Запуск постоянного прослушивания ESP32...")

        async def handle_start(conversation_id: str, flags: int,
                               audio_settings: aioesphomeapi.VoiceAssistantAudioSettings,
                               wake_word_phrase: str | None):
            self.conversation_id = conversation_id
            self.is_listening = True
            print(f"\n🎙️ Ассистент активирован:")
            print(f"   Conversation ID: {conversation_id}")
            print(f"   Wake word: {wake_word_phrase}")

            # Возвращаем 0, так как не используем TCP
            return 0

        async def handle_stop(expected_stop: bool):
            print(f"\n⏹️ Ассистент остановлен")
            self.conversation_id = None
            self.is_listening = False

        async def handle_audio_wrapper(audio_data: bytes):
            """Обработка аудио данных - сохраняем только если идет запись сегмента"""
            await self._handle_audio(audio_data)
            await asyncio.sleep(0)

        # Подписываемся на события ассистента
        self.cli.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_stop=handle_stop,
            handle_audio=handle_audio_wrapper,
        )

    async def _handle_audio(self, audio_data: bytes):
        """Обработка входящих аудио данных - сохраняем только во время записи сегмента"""
        if self.is_recording_segment:
            async with self.audio_lock:
                self.segment_buffer.extend(audio_data)

            # Проверяем, не истекло ли время записи
            if self.segment_start_time and (
                    datetime.now().timestamp() - self.segment_start_time >= self.segment_duration):
                await self._stop_segment_recording()

    async def start_segment_recording(self):
        """Начало записи 5-секундного сегмента"""
        if self.is_recording_segment:
            print("⚠️ Запись сегмента уже идет")
            return None

        if not self.is_listening:
            print("⚠️ ESP32 не слушает - активируйте голосовой помощник на устройстве")
            return None

        self.is_recording_segment = True
        self.segment_buffer = bytearray()
        self.segment_start_time = datetime.now().timestamp()

        # Создаем файл для записи
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_segment_filename = f"recording_{timestamp}.wav"
        filepath = os.path.join(RECORDINGS_DIR, self.current_segment_filename)

        print(f"🎙️ Начало записи сегмента: 5 секунд -> {self.current_segment_filename}")
        return self.current_segment_filename

    async def _stop_segment_recording(self):
        """Остановка записи сегмента и сохранение файла"""
        if not self.is_recording_segment:
            return None

        self.is_recording_segment = False
        filename = self.current_segment_filename

        # Сохраняем данные
        async with self.audio_lock:
            audio_data = self.segment_buffer.copy()
            self.segment_buffer.clear()

        if audio_data:
            success = await self._save_audio_data(audio_data, os.path.join(RECORDINGS_DIR, filename))
            if success:
                print(f"💾 Сегмент сохранен: {filename} ({len(audio_data)} bytes)")
                return filename
            else:
                print(f"❌ Ошибка сохранения сегмента: {filename}")
                return None
        else:
            print("⚠️ Нет данных для сохранения")
            return None

    async def stop_segment_recording(self):
        """Остановка записи сегмента по команде"""
        return await self._stop_segment_recording()

    async def get_recording_status(self):
        """Получение статуса записи"""
        if self.is_recording_segment and self.segment_start_time:
            elapsed = datetime.now().timestamp() - self.segment_start_time
            remaining = max(0, self.segment_duration - elapsed)
            return {
                'recording': True,
                'elapsed': round(elapsed, 1),
                'remaining': round(remaining, 1),
                'total_duration': self.segment_duration,
                'buffer_size': len(self.segment_buffer)
            }
        else:
            return {
                'recording': False,
                'elapsed': 0,
                'remaining': 0,
                'total_duration': self.segment_duration,
                'buffer_size': 0
            }

    async def _save_audio_data(self, audio_data, filename):
        """Сохранение аудио данных в WAV файл"""
        if not audio_data:
            return False

        try:
            with wave.open(filename, 'wb') as wav_file:
                wav_file.setnchannels(1)  # моно
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(16000)  # 16 kHz
                wav_file.writeframes(audio_data)
            return True
        except Exception as e:
            print(f"❌ Ошибка сохранения WAV файла {filename}: {e}")
            return False

    # Веб-интерфейс
    async def setup_web_interface(self):
        """Настройка веб-интерфейса"""
        self.app = web.Application()

        # Настраиваем маршруты
        self.app.router.add_get('/', self._handle_index)
        self.app.router.add_post('/start_recording', self._handle_start_recording)
        self.app.router.add_post('/stop_recording', self._handle_stop_recording)
        self.app.router.add_get('/status', self._handle_status)
        self.app.router.add_get('/download/{filename}', self._handle_download)
        self.app.router.add_static('/recordings/', RECORDINGS_DIR)

    async def _handle_index(self, request):
        """Главная страница"""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Запись аудио с ESP32</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; }
                .container { max-width: 600px; margin: 0 auto; }
                button { 
                    padding: 15px 30px; 
                    font-size: 16px; 
                    margin: 10px; 
                    border: none; 
                    border-radius: 5px; 
                    cursor: pointer; 
                }
                #startBtn { background: #27ae60; color: white; }
                #stopBtn { background: #e74c3c; color: white; display: none; }
                .status { 
                    padding: 20px; 
                    margin: 20px 0; 
                    border-radius: 5px; 
                }
                .recording { background: #ffebee; color: #e74c3c; }
                .ready { background: #e8f5e8; color: #27ae60; }
                .waiting { background: #fff3cd; color: #856404; }
                .timer { font-size: 24px; font-weight: bold; margin: 10px 0; }
                .instructions { background: #f8f9fa; padding: 15px; border-radius: 5px; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🎧 Запись аудио с ESP32</h1>

                <div class="instructions">
                    <p><strong>Режим работы:</strong> Постоянное прослушивание ESP32</p>
                    <p>Нажмите "Начать запись" для сохранения 5-секундного отрезка</p>
                    <p>Запись автоматически остановится через 5 секунд</p>
                </div>

                <button id="startBtn" onclick="startRecording()">Начать запись (5 сек)</button>
                <button id="stopBtn" onclick="stopRecording()">Остановить запись</button>

                <div id="status" class="status waiting">
                    ⏳ Ожидание активации ESP32... (скажите wake word на устройстве)
                </div>

                <div id="timer" class="timer" style="display:none;"></div>

                <div id="downloadLink" style="display:none; margin-top: 20px;">
                    <a id="downloadAnchor" style="padding: 10px 20px; background: #3498db; color: white; text-decoration: none; border-radius: 5px;">
                        📥 Скачать запись
                    </a>
                </div>
            </div>

            <script>
                let recording = false;
                let statusInterval = null;

                function updateStatus() {
                    fetch('/status')
                        .then(response => response.json())
                        .then(data => {
                            // Обновляем статус подключения
                            if (data.listening) {
                                if (data.recording) {
                                    document.getElementById('status').className = 'status recording';
                                    document.getElementById('status').innerHTML = '🎙️ Идет запись...';
                                    document.getElementById('timer').style.display = 'block';
                                    document.getElementById('timer').textContent = 
                                        data.elapsed + ' / ' + data.total_duration + ' сек';

                                    // Автоматическая остановка по истечении времени
                                    if (data.remaining <= 0) {
                                        stopRecording();
                                    }
                                } else {
                                    document.getElementById('status').className = 'status ready';
                                    document.getElementById('status').innerHTML = '✅ Готов к записи (ESP32 активен)';
                                    document.getElementById('timer').style.display = 'none';
                                }
                            } else {
                                document.getElementById('status').className = 'status waiting';
                                document.getElementById('status').innerHTML = '⏳ Ожидание активации ESP32... (скажите wake word на устройстве)';
                                document.getElementById('timer').style.display = 'none';
                            }
                        });
                }

                function startRecording() {
                    fetch('/start_recording', {method: 'POST'})
                        .then(response => response.json())
                        .then(data => {
                            if(data.success) {
                                recording = true;
                                document.getElementById('startBtn').style.display = 'none';
                                document.getElementById('stopBtn').style.display = 'inline-block';
                                document.getElementById('downloadLink').style.display = 'none';

                                // Запускаем обновление статуса
                                statusInterval = setInterval(updateStatus, 100);
                            } else {
                                alert('Ошибка: ' + data.error);
                            }
                        });
                }

                function stopRecording() {
                    fetch('/stop_recording', {method: 'POST'})
                        .then(response => response.json())
                        .then(data => {
                            recording = false;
                            if(statusInterval) clearInterval(statusInterval);

                            document.getElementById('startBtn').style.display = 'inline-block';
                            document.getElementById('stopBtn').style.display = 'none';
                            document.getElementById('timer').style.display = 'none';

                            if(data.filename) {
                                const anchor = document.getElementById('downloadAnchor');
                                anchor.href = '/download/' + data.filename;
                                anchor.textContent = '📥 Скачать ' + data.filename;
                                document.getElementById('downloadLink').style.display = 'block';
                            }
                        });
                }

                // Автоматическое обновление статуса каждые 2 секунды
                setInterval(updateStatus, 2000);
                updateStatus(); // Первоначальный запрос статуса
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')

    async def _handle_start_recording(self, request):
        """Начало записи сегмента"""
        filename = await self.start_segment_recording()
        if filename:
            return web.json_response({
                'success': True,
                'filename': filename
            })
        else:
            return web.json_response({
                'success': False,
                'error': 'Не удалось начать запись. Убедитесь, что ESP32 активен.'
            })

    async def _handle_stop_recording(self, request):
        """Остановка записи сегмента"""
        filename = await self.stop_segment_recording()
        if filename:
            return web.json_response({
                'success': True,
                'filename': filename
            })
        else:
            return web.json_response({
                'success': False,
                'error': 'Нет активной записи'
            })

    async def _handle_status(self, request):
        """Получение статуса системы"""
        recording_status = await self.get_recording_status()
        status_data = {
            'listening': self.is_listening,
            'recording': recording_status['recording'],
            'elapsed': recording_status['elapsed'],
            'remaining': recording_status['remaining'],
            'total_duration': recording_status['total_duration'],
            'buffer_size': recording_status['buffer_size']
        }
        return web.json_response(status_data)

    async def _handle_download(self, request):
        """Скачивание файла"""
        filename = request.match_info['filename']
        filepath = os.path.join(RECORDINGS_DIR, filename)

        if os.path.exists(filepath):
            return web.FileResponse(filepath)
        else:
            return web.Response(text="File not found", status=404)

    async def start_web_server(self, host='0.0.0.0', port=5000):
        """Запуск веб-сервера"""
        await self.setup_web_interface()

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        print(f"🌐 Веб-интерфейс запущен: http://{host}:{port}")

    async def disconnect(self):
        """Отключение от устройства"""
        try:
            await self.stop_segment_recording()
            if self.cli:
                await self.cli.disconnect()
                print("🔌 Отключено от устройства")

            if self.site:
                await self.site.stop()
            if self.runner:
                await self.runner.cleanup()

        except Exception as e:
            print(f"❌ Ошибка при отключении: {e}")


async def main():
    # Конфигурация
    HOST = "192.168.0.121"  # IP устройства ESP32
    PORT = 6053
    PASSWORD = ""

    assistant = ESPHomeVoiceAssistant(HOST, PORT, PASSWORD)

    try:
        # Подключаемся к ESP32
        if not await assistant.connect():
            return

        # Запускаем постоянное прослушивание
        assistant.start_voice_assistant()

        # Запускаем веб-сервер
        await assistant.start_web_server()

        print("\n🎯 СИСТЕМА ЗАПУЩЕНА!")
        print("   Режим: Постоянное прослушивание ESP32")
        print("   Действия:")
        print("   1. Активируйте голосовой помощник на ESP32 (кнопка или wake word)")
        print("   2. Откройте веб-интерфейс: http://localhost:5000")
        print("   3. Нажимайте 'Начать запись' для сохранения 5-секундных отрезков")
        print("   4. Запись автоматически остановится через 5 секунд")
        print("\n   Для выхода нажмите Ctrl+C")

        # Основной цикл
        while True:
            await asyncio.sleep(1)

            # Периодически показываем статус
            if assistant.is_listening and not assistant.is_recording_segment:
                print("✅ ESP32 активен - готов к записи", end='\r')
            elif not assistant.is_listening:
                print("⏳ Ожидание активации ESP32...", end='\r')
            elif assistant.is_recording_segment:
                status = await assistant.get_recording_status()
                print(f"🎙️ Запись: {status['elapsed']:.1f}/{status['total_duration']} сек", end='\r')

    except KeyboardInterrupt:
        print("\n\n🛑 Остановка по запросу пользователя...")
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🧹 Завершение работы...")
        await assistant.disconnect()
        print("👋 Работа завершена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Программа завершена")