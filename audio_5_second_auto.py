import aioesphomeapi
import asyncio
import wave
from datetime import datetime
import logging
import os
import json
import numpy as np
from scipy.signal import resample

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
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

        # Для управления автоматической записью
        self.is_listening = False
        self.is_auto_recording = False
        self.recording_buffer = bytearray()
        self.recording_start_time = None
        self.segment_duration = 5  # 5 секунд
        self.current_segment_filename = None
        self.audio_lock = asyncio.Lock()

        # Для отслеживания сегментов
        self.segment_counter = 0
        self.auto_record_task = None

    async def connect(self):
        """Подключение к устройству"""
        self.cli = aioesphomeapi.APIClient(self.host, self.port, self.password)

        try:
            await self.cli.connect(login=True)
            self.is_connected = True
            logger.info("✅ Успешно подключено к устройству")

            device_info = await self.cli.device_info()
            logger.info(f"Устройство: {device_info.name}, Версия: {device_info.esphome_version}")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка подключения: {e}")
            self.is_connected = False
            return False

    def start_voice_assistant(self):
        """Запуск голосового ассистента для постоянного прослушивания"""
        logger.info("\n🎤 Запуск постоянного прослушивания ESP32...")

        async def handle_start(conversation_id: str, flags: int,
                               audio_settings: aioesphomeapi.VoiceAssistantAudioSettings,
                               wake_word_phrase: str | None):
            """Обработка начала разговора"""
            self.conversation_id = conversation_id
            self.is_listening = True
            logger.info(f"\n🎙️ Ассистент активирован:")
            logger.info(f"   Conversation ID: {conversation_id}")
            logger.info(f"   Wake word: {wake_word_phrase}")

            # Запускаем автоматическую запись
            await self.start_auto_recording()

            # Возвращаем 0, так как не используем TCP
            return 0

        async def handle_stop(expected_stop: bool):
            """Обработка остановки разговора"""
            logger.info(f"\n⏹️ Ассистент остановлен")
            self.conversation_id = None
            self.is_listening = False

            # Останавливаем автоматическую запись
            await self.stop_auto_recording()

        async def handle_audio_wrapper(audio_data: bytes):
            """Обработка аудио данных"""
            await self._handle_audio(audio_data)

        # Подписываемся на события ассистента
        self.cli.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_stop=handle_stop,
            handle_audio=handle_audio_wrapper,
        )

    async def _handle_audio(self, audio_data: bytes):
        """Обработка входящих аудио данных"""
        if self.is_auto_recording:
            async with self.audio_lock:
                self.recording_buffer.extend(audio_data)

    async def start_auto_recording(self):
        """Запуск автоматической записи сегментов"""
        if self.is_auto_recording:
            logger.warning("⚠️ Автоматическая запись уже запущена")
            return

        self.is_auto_recording = True
        self.segment_counter = 0

        # Запускаем задачу для автоматической записи сегментов
        self.auto_record_task = asyncio.create_task(self._auto_record_loop())
        logger.info("🎙️ Автоматическая запись запущена")

    async def stop_auto_recording(self):
        """Остановка автоматической записи"""
        if not self.is_auto_recording:
            return

        self.is_auto_recording = False

        # Останавливаем задачу
        if self.auto_record_task:
            self.auto_record_task.cancel()
            try:
                await self.auto_record_task
            except asyncio.CancelledError:
                pass

        # Сохраняем оставшиеся данные
        await self._save_current_segment()
        logger.info("⏹️ Автоматическая запись остановлена")

    async def _auto_record_loop(self):
        """Цикл автоматической записи сегментов"""
        logger.info("🔄 Запуск цикла автоматической записи")

        try:
            while self.is_auto_recording and self.is_listening:
                # Ждем заданное время для записи сегмента
                await asyncio.sleep(self.segment_duration)

                # Сохраняем текущий сегмент и начинаем новый
                if self.is_auto_recording and self.is_listening:
                    await self._save_current_segment()
                    self.segment_counter += 1
                    logger.info(f"📁 Сегмент #{self.segment_counter} сохранен, начинается запись следующего...")

        except asyncio.CancelledError:
            logger.info("🔄 Цикл автоматической записи прерван")
        except Exception as e:
            logger.error(f"❌ Ошибка в цикле записи: {e}")

    async def _save_current_segment(self):
        """Сохранение текущего сегмента"""
        if not self.recording_buffer:
            logger.debug("⚠️ Нет данных для сохранения")
            return

        # Создаем имя файла для текущего сегмента
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"segment_{timestamp}_part{self.segment_counter + 1}.wav"
        filepath = os.path.join(RECORDINGS_DIR, filename)

        # Получаем данные из буфера
        async with self.audio_lock:
            audio_data = self.recording_buffer.copy()
            self.recording_buffer.clear()

        # Сохраняем файл
        if audio_data:
            success = await self._save_audio_data(audio_data, filepath)
            if success:
                logger.info(f"💾 Сохранен сегмент {filename} ({len(audio_data)} байт)")
                self.current_segment_filename = filename
            else:
                logger.error(f"❌ Ошибка сохранения сегмента {filename}")
        else:
            logger.debug("⚠️ Пустой буфер, пропускаем сохранение")

    async def _save_audio_data(self, audio_data, filename):
        """Сохранение аудио данных в WAV файл с ресемплингом до 8 кГц"""
        if not audio_data:
            return False

        try:
            # Преобразуем байты в numpy массив (16-bit signed integers)
            raw_audio = np.frombuffer(audio_data, dtype=np.int16)

            # Текущая частота дискретизации (предполагается 16000 Гц)
            original_rate = 16000
            target_rate = 8000

            # Вычисляем новую длину массива после ресемплинга
            num_samples = int(len(raw_audio) * target_rate / original_rate)

            # Ресемплинг
            resampled_audio = resample(raw_audio, num_samples)

            # Обрезаем до чётного числа семплов (если нужно для WAV)
            if len(resampled_audio) % 2 != 0:
                resampled_audio = resampled_audio[:-1]

            # Преобразуем обратно в байты
            resampled_bytes = resampled_audio.astype(np.int16).tobytes()

            # Сохраняем в WAV файл с частотой 8 кГц
            with wave.open(filename, 'wb') as wav_file:
                wav_file.setnchannels(1)  # моно
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(target_rate)  # 8 kHz
                wav_file.writeframes(resampled_bytes)
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения WAV файла {filename}: {e}")
            return False

    async def get_status(self):
        """Получение статуса системы"""
        return {
            'connected': self.is_connected,
            'listening': self.is_listening,
            'auto_recording': self.is_auto_recording,
            'segment_counter': self.segment_counter,
            'buffer_size': len(self.recording_buffer),
            'segment_duration': self.segment_duration
        }

    async def disconnect(self):
        """Отключение от устройства"""
        try:
            # Останавливаем автоматическую запись
            await self.stop_auto_recording()

            # Отключаемся от устройства
            if self.cli:
                await self.cli.disconnect()
                logger.info("🔌 Отключено от устройства")

        except Exception as e:
            logger.error(f"❌ Ошибка при отключении: {e}")


async def main():
    # Конфигурация
    HOST = "192.168.0.103"  # IP устройства ESP32
    PORT = 6053
    PASSWORD = ""  # Оставьте пустым, если не установлен

    assistant = ESPHomeVoiceAssistant(HOST, PORT, PASSWORD)

    try:
        # Подключаемся к ESP32
        logger.info(f"Подключаемся к ESP32 на {HOST}:{PORT}...")
        if not await assistant.connect():
            logger.error("Не удалось подключиться к ESP32")
            return

        # Запускаем постоянное прослушивание
        assistant.start_voice_assistant()

        print("\n" + "=" * 60)
        print("🎯 СИСТЕМА ЗАПУЩЕНА!")
        print("=" * 60)
        print("\n📋 РЕЖИМ РАБОТЫ: АВТОМАТИЧЕСКАЯ ЗАПИСЬ")
        print("\n🔧 НАСТРОЙКИ:")
        print(f"   • Длительность сегмента: {assistant.segment_duration} секунд")
        print(f"   • Папка для записей: {os.path.abspath(RECORDINGS_DIR)}")
        print(f"   • Частота дискретизации: 8 кГц (после ресемплинга)")
        print("\n🎤 ИНСТРУКЦИЯ:")
        print("   1. Активируйте голосовой помощник на ESP32:")
        print("      - Скажите wake word (например, 'Alexa', 'Hey Google')")
        print("      - Или нажмите кнопку активации на устройстве")
        print("\n   2. После активации начнется автоматическая запись:")
        print("      - Каждые 5 секунд будет сохраняться новый файл")
        print("      - Все файлы сохраняются в папке 'esp32_recordings'")
        print("      - Имена файлов содержат дату, время и номер сегмента")
        print("      - Файлы сохраняются с частотой 8 кГц после ресемплинга")
        print("\n   3. Когда разговор завершится, запись остановится автоматически")
        print("\n   4. Для выхода нажмите Ctrl+C")
        print("=" * 60 + "\n")

        # Основной цикл - просто ждем
        while True:
            await asyncio.sleep(1)

            # Периодически показываем статус
            status = await assistant.get_status()
            if status['listening'] and status['auto_recording']:
                print(f"🎙️ Запись... Сегмент #{status['segment_counter']} | Буфер: {status['buffer_size']} байт",
                      end='\r')
            elif not status['listening']:
                print("⏳ Ожидание активации ESP32 (скажите wake word)...", end='\r')
            else:
                print(f"✅ Готов к записи | Подключено: {status['connected']}", end='\r')

    except KeyboardInterrupt:
        print("\n\n🛑 Остановка по запросу пользователя...")
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🧹 Завершение работы...")
        await assistant.disconnect()
        print("👋 Работа завершена")
        print(f"📁 Записи сохранены в: {os.path.abspath(RECORDINGS_DIR)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Программа завершена")