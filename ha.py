import aioesphomeapi
import asyncio
import wave
from datetime import datetime
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ESPHomeVoiceAssistant:
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password
        self.cli = None
        self.is_connected = False
        self.conversation_id = None
        self.current_audio_data = bytearray()
        self.segment_timer = None
        self.segment_count = 0
        self.is_recording = False
        self.audio_filename = None
        self.audio_lock = asyncio.Lock()

    async def connect(self):
        """Подключение к устройству"""
        self.cli = aioesphomeapi.APIClient(self.host, self.port, self.password)

        try:
            await self.cli.connect(login=True)
            self.is_connected = True
            print("✅ Успешно подключено к устройству")
            print(f"API version: {self.cli.api_version}")

            device_info = await self.cli.device_info()
            print(f"Device: {device_info.name}, Version: {device_info.esphome_version}")

        except Exception as e:
            print(f"❌ Ошибка подключения: {e}")
            self.is_connected = False

    def subscribe_to_logs(self):
        """Подписка на логи устройства"""

        def log_callback(msg):
            print(f"[DEVICE LOG] {msg.message}", end='')

        self.cli.subscribe_logs(log_callback)

    def start_voice_assistant(self):
        """Запуск голосового ассистента"""
        print("\n🎤 Инициализация голосового ассистента...")

        async def handle_start(conversation_id: str, flags: int,
                               audio_settings: aioesphomeapi.VoiceAssistantAudioSettings,
                               wake_word_phrase: str | None):
            self.conversation_id = conversation_id
            print(f"\n🎙️ Ассистент запущен:")
            print(f"   Conversation ID: {conversation_id}")
            print(f"   Flags: {flags}")
            print(f"   Wake word: {wake_word_phrase}")
            print(f"   Audio settings: noise_suppression_level={audio_settings.noise_suppression_level}, "
                  f"auto_gain={audio_settings.auto_gain}, volume_multiplier={audio_settings.volume_multiplier}")

            # Начинаем запись аудио
            await self._start_audio_recording()

            # Возвращаем 0, так как не используем TCP
            return 0

        async def handle_stop(expected_stop: bool):
            print(f"\n⏹️ Ассистент остановлен (expected: {expected_stop})")
            self.conversation_id = None
            await self._stop_audio_recording()

        async def handle_audio_wrapper(audio_data: bytes):
            """Обертка для обработки аудио, которая возвращает корутину"""
            await self._handle_audio(audio_data)
            # Возвращаем небольшую задержку, чтобы удовлетворить требования библиотеки
            await asyncio.sleep(0)

        # Подписываемся на события ассистента с оберткой для аудио
        self.cli.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_stop=handle_stop,
            handle_audio=handle_audio_wrapper,  # Используем обертку
        )

    async def _handle_audio(self, audio_data: bytes):
        """Обработка входящих аудио данных через API"""
        if self.is_recording:
            async with self.audio_lock:
                self.current_audio_data.extend(audio_data)
            # Выводим сообщение только иногда, чтобы не засорять вывод
            if len(self.current_audio_data) % 10240 == 0:  # Каждые ~10 KB
                print(f"📥 Received audio: {len(self.current_audio_data)} bytes total")

    async def _start_audio_recording(self):
        """Начинаем запись аудио"""
        if self.is_recording:
            await self._stop_audio_recording()

        self.is_recording = True
        self.current_audio_data = bytearray()
        self.segment_count = 0

        # Создаем основной файл для записи
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.audio_filename = f"audio_capture_{timestamp}.wav"

        print(f"🔊 Начата запись аудио в файл: {self.audio_filename}")

        # Запускаем таймер для сегментирования
        await self._start_audio_segment_timer()

    async def _stop_audio_recording(self):
        """Останавливаем запись аудио"""
        if not self.is_recording:
            return

        self.is_recording = False

        # Останавливаем таймер
        await self._stop_audio_segment_timer()

        # Сохраняем оставшиеся данные
        if self.current_audio_data:
            async with self.audio_lock:
                audio_data = self.current_audio_data.copy()
                self.current_audio_data.clear()

            if audio_data:
                await self._save_audio_data(audio_data, self.audio_filename)
                print(f"💾 Запись завершена. Итоговый файл: {self.audio_filename} ({len(audio_data)} bytes)")

    async def _start_audio_segment_timer(self):
        """Запуск таймера для сохранения аудио сегментов"""
        if self.segment_timer:
            self.segment_timer.cancel()

        self.segment_timer = asyncio.create_task(self._save_audio_segment_periodically())

    async def _stop_audio_segment_timer(self):
        """Остановка таймера сегментирования аудио"""
        if self.segment_timer:
            self.segment_timer.cancel()
            try:
                await self.segment_timer
            except asyncio.CancelledError:
                pass
            self.segment_timer = None

    async def _save_audio_segment_periodically(self):
        """Периодическое сохранение аудио сегментов"""
        segment_duration = 10  # секунды

        try:
            while self.is_recording:
                await asyncio.sleep(segment_duration)

                if self.is_recording and self.current_audio_data:
                    self.segment_count += 1
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    segment_filename = f"audio_segment_{timestamp}_part{self.segment_count}.wav"

                    # Сохраняем текущие данные
                    async with self.audio_lock:
                        audio_data_copy = self.current_audio_data.copy()
                        self.current_audio_data.clear()

                    if audio_data_copy:
                        await self._save_audio_data(audio_data_copy, segment_filename)
                        print(f"💾 Сохранен аудио сегмент {self.segment_count} ({len(audio_data_copy)} bytes)")
                else:
                    status = "не записывается" if not self.is_recording else "нет данных"
                    print(f"⏭️  Пропуск сегмента {self.segment_count + 1} - {status}")

        except asyncio.CancelledError:
            print("⏹️  Таймер сегментирования аудио остановлен")
        except Exception as e:
            print(f"❌ Ошибка в таймере сегментирования: {e}")

    async def _save_audio_data(self, audio_data, filename):
        """Сохранение аудио данных в WAV файл"""
        if not audio_data:
            return

        try:
            with wave.open(filename, 'wb') as wav_file:
                wav_file.setnchannels(1)  # моно
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(16000)  # 16 kHz
                wav_file.writeframes(audio_data)
            print(f"💾 WAV файл сохранен: {filename} ({len(audio_data)} bytes)")
            return True
        except Exception as e:
            print(f"❌ Ошибка сохранения WAV файла {filename}: {e}")
            return False

    async def disconnect(self):
        """Отключение от устройства"""
        try:
            await self._stop_audio_recording()
            if self.cli:
                await self.cli.disconnect()
                print("🔌 Отключено от устройства")
        except Exception as e:
            print(f"❌ Ошибка при отключении: {e}")


async def main():
    # Конфигурация
    HOST = "192.168.0.103"  # IP устройства ESP32
    PORT = 6053
    PASSWORD = ""

    assistant = ESPHomeVoiceAssistant(HOST, PORT, PASSWORD)

    try:
        await assistant.connect()
        if not assistant.is_connected:
            return

        assistant.subscribe_to_logs()
        assistant.start_voice_assistant()

        print("\n🤖 Система готова к работе!")
        print("   Скажите wake word на устройстве или нажмите Ctrl+C для выхода")
        print("   Аудио будет сохраняться в файлы каждые 10 секунд")

        # Основной цикл
        while True:
            try:
                await asyncio.sleep(0.1)  # Короткая задержка для обработки событий
            except KeyboardInterrupt:
                print("\n🛑 Остановка по запросу пользователя...")
                break

    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("🧹 Завершение работы...")
        await assistant.disconnect()
        print("👋 Работа завершена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Программа завершена")