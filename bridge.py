import aioesphomeapi
import asyncio
import threading
import queue
import logging
import pjsua2 as pj
import time
import struct
import numpy as np
import scipy.signal  # Импортируем для ресемплинга

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class SIPCall(pj.Call):
    """Класс для обработки SIP звонков"""

    def __init__(self, acc, call_id=-1, bridge=None):
        pj.Call.__init__(self, acc, call_id)
        self.bridge = bridge
        self.connected = False
        self.audio_media = None

    def onCallState(self, prm):
        ci = self.getInfo()
        logger.info(f"📞 Статус звонка: {ci.stateText}")

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.connected = True
            logger.info("✅ Звонок принят! Разговор начался...")
            if self.bridge:
                self.bridge.call_connected = True
                asyncio.run_coroutine_threadsafe(
                    self.bridge.on_call_connected(),
                    self.bridge.loop
                )

        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.connected = False
            if self.bridge:
                self.bridge.call_connected = False
            logger.info("❌ Звонок завершен")

    def onCallMediaState(self, prm):
        """Callback при изменении состояния медиа"""
        ci = self.getInfo()
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                # Получаем аудио медиа для передачи аудио в SIP
                self.audio_media = self.getAudioMedia(mi.index)
                logger.info("🎵 Аудио медиа активировано для передачи")
                if self.bridge:
                    asyncio.run_coroutine_threadsafe(
                        self.bridge.setup_audio_bridge(self.audio_media),
                        self.bridge.loop
                    )


class SIPAudioMediaPort(pj.AudioMediaPort):
    """
    Пользовательский AudioMediaPort для подачи аудио из ESP32 в SIP звонок.
    Аудио из очереди (предположительно 16-bit PCM @ esp_rate) ресемплируется до 8kHz 16-bit PCM и передается в SIP.
    """

    def __init__(self, esp_to_sip_queue, esp_clock_rate=16000):  # Принимаем частоту ESP
        pj.AudioMediaPort.__init__(self)
        self.esp_to_sip_queue = esp_to_sip_queue
        self.esp_clock_rate = esp_clock_rate  # Частота дискретизации ESP (обычно 16000)
        self.sip_clock_rate = 8000  # Целевая частота для SIP
        self.samples_per_20ms_esp = int(self.esp_clock_rate * 0.020)  # Сэмплов за 20 мс на частоте ESP
        self.bytes_per_20ms_esp = self.samples_per_20ms_esp * 2  # Байтов за 20 мс на частоте ESP (16-bit)
        self.samples_per_20ms_sip = int(self.sip_clock_rate * 0.020)  # 160 сэмплов за 20 мс на 8kHz
        self.bytes_per_20ms_sip = self.samples_per_20ms_sip * 2  # 320 байт за 20 мс на 8kHz (16-bit)
        logger.info(f"🔧 SIPAudioMediaPort: ESP rate={self.esp_clock_rate} Hz, SIP rate={self.sip_clock_rate} Hz")

    def onFrameRequested(self, frame):
        """
        Вызывается PJSIP когда ему нужны аудио данные для отправки в звонок.
        Ожидаем 16-bit PCM из очереди с частотой ESP, ресемплируем до 8kHz 16-bit PCM, передаем в SIP.
        """
        # Проверяем, есть ли место в буфере
        if frame.size == 0:
            return

        # Инициализируем буфер frame.buf как пустой байтовый вектор
        frame.buf = pj.ByteVector()
        frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
        frame.size = 0  # Пока что размер ноль

        # Размер в байтах, который нам нужен в итоге для SIP (160 сэмплов * 2 байта = 320 байт для 8kHz 16-bit 20ms фрейма)
        needed_bytes = self.bytes_per_20ms_sip  # 320 байт

        # Пытаемся получить аудио данные из очереди (ожидаем 16-bit PCM с частотой ESP)
        raw_audio_bytes = b''
        try:
            # Получаем блок данных из очереди (предполагаем, что ESP отправляет порции по 20мс или кратные)
            raw_audio_bytes = self.esp_to_sip_queue.get_nowait()
            logger.debug(f"📥 Получено {len(raw_audio_bytes)} байт из очереди ESP32")
        except queue.Empty:
            # Если очередь пуста, отправляем тишину (нули, соответствующие 16-битному формату SIP)
            raw_audio_bytes = b'\x00' * self.bytes_per_20ms_esp  # Тишина для 20мс ESP (обычно 640 байт)
            logger.debug(f"📥 Очередь пуста, получаем тишину ESP ({self.bytes_per_20ms_esp} байт)")

        # --- РЕСЕМПЛИНГ ---
        # Преобразуем байты в numpy array 16-bit signed int
        try:
            raw_audio_int16 = np.frombuffer(raw_audio_bytes, dtype=np.int16)
            logger.debug(f"🔍 raw_audio_int16 shape: {raw_audio_int16.shape}")
        except Exception as e:
            logger.error(f"❌ Ошибка преобразования raw_audio_bytes в int16: {e}")
            # Отправляем тишину в случае ошибки
            raw_audio_int16 = np.zeros(self.samples_per_20ms_esp, dtype=np.int16)

        # Ресемплинг: ESP_rate -> SIP_rate
        # scipy.signal.resample_poly делает это эффективно
        # Вход: сигнал с частотой self.esp_clock_rate
        # Выход: сигнал с частотой self.sip_clock_rate
        # Коэффициенты: up = self.sip_clock_rate, down = self.esp_clock_rate
        # Для 16000 -> 8000: up=1, down=2 (downsampling by 2)
        # Для 48000 -> 8000: up=1, down=6
        # scipy.signal.resample_poly(signal, up, down, ...)
        # up: upsampling factor
        # down: downsampling factor
        up_factor = self.sip_clock_rate
        down_factor = self.esp_clock_rate
        # Найдем НОД для упрощения коэффициентов
        import math
        gcd_val = math.gcd(up_factor, down_factor)
        up_simplified = up_factor // gcd_val
        down_simplified = down_factor // gcd_val
        logger.debug(
            f"🔍 Ресемплинг: {self.esp_clock_rate} -> {self.sip_clock_rate} (up={up_simplified}, down={down_simplified})")

        try:
            resampled_int16 = scipy.signal.resample_poly(raw_audio_int16, up_simplified, down_simplified)
            logger.debug(f"🔍 resampled_int16 shape: {resampled_int16.shape}")
        except Exception as e:
            logger.error(f"❌ Ошибка ресемплинга: {e}")
            # Отправляем тишину в случае ошибки
            resampled_int16 = np.zeros(self.samples_per_20ms_sip, dtype=np.int16)

        # Преобразуем ресемпленный numpy array обратно в байты
        resampled_audio_bytes = resampled_int16.astype(np.int16).tobytes()

        # Теперь resampled_audio_bytes содержит 16-битные сэмплы с частотой 8kHz
        # Если длина больше needed_bytes, обрежем
        if len(resampled_audio_bytes) > needed_bytes:
            resampled_audio_bytes = resampled_audio_bytes[:needed_bytes]
        elif len(resampled_audio_bytes) < needed_bytes:
            # Дополняем тишиной (0x00) до нужного размера (320 байт)
            resampled_audio_bytes += b'\x00' * (needed_bytes - len(resampled_audio_bytes))

        # Заполнение frame.buf (16-битные данные, 8kHz)
        frame.buf.resize(len(resampled_audio_bytes))
        for i, byte_val in enumerate(resampled_audio_bytes):
            frame.buf[i] = byte_val

        frame.size = len(resampled_audio_bytes)
        logger.debug(f"📤 Отправлено {frame.size} байт в SIP звонок (16-bit, 8kHz после ресемплинга)")

    def onFrameReceived(self, frame):
        """
        Вызывается когда этот порт получает аудио из SIP звонка.
        (В текущем сценарии не используется, так как мы только передаем из ESP в SIP).
        """
        # logger.debug(f"📥 Получено {frame.size} байт из SIP звонка (игнорируется)")
        pass  # Игнорируем получение из SIP, так как мы только передаем

    # Убран метод close, так как больше не сохраняем WAV


class ESP32SIPAudioBridge:
    """Главный класс моста ESP32 -> SIP (только передача звука с микрофона ESP32 в SIP)"""

    def __init__(self, esp_host, esp_port, esp_password, sip_target_uri, esp_clock_rate=16000):
        # ESP32 параметры
        self.esp_host = esp_host
        self.esp_port = esp_port
        self.esp_password = esp_password
        self.cli = None
        self.voice_assistant_active = False
        self.conversation_id = None
        self.unsubscribe_callback = None

        # SIP параметры
        self.sip_target_uri = sip_target_uri
        self.ep = None
        self.acc = None
        self.call = None
        self.call_connected = False
        self.sip_audio_media = None
        self.sip_audio_port = None  # Новый атрибут для хранения порта

        # Очередь для аудио данных с ESP32
        self.esp_to_sip_queue = queue.Queue(maxsize=50)  # Ограничиваем размер очереди

        # Event loop для асинхронных операций
        self.loop = asyncio.get_event_loop()

        # Флаги для управления состоянием
        self.device_activated = False
        self.audio_bridge_setup = False

        # Частота дискретизации ESP (часто 16000 по умолчанию в ESPHome Audio)
        self.esp_clock_rate = esp_clock_rate

        # Оптимизация очереди
        self.esp_to_sip_queue = queue.Queue(maxsize=20)  # Увеличиваем буфер

        # Статистика для мониторинга
        self.audio_stats = {
            'frames_sent': 0,
            'queue_drops': 0,
            'resample_errors': 0
        }

    async def monitor_audio_quality(self):
        """Мониторинг качества аудио"""
        while self.call_connected:
            await asyncio.sleep(10)
            queue_size = self.esp_to_sip_queue.qsize()
            logger.info(f"📊 Статистика аудио: очередь={queue_size}, "
                        f"фреймы={self.audio_stats['frames_sent']}, "
                        f"потери={self.audio_stats['queue_drops']}")

    async def ensure_esp32_connection(self):
        """Убедиться, что подключение к ESP32 активно"""
        try:
            if self.cli is None:
                return await self.connect_esp32()

            # Проверяем, активно ли соединение
            try:
                await self.cli.device_info()
                return True
            except Exception:
                logger.warning("🔌 Соединение с ESP32 разорвано, переподключаемся...")
                return await self.connect_esp32()

        except Exception as e:
            logger.error(f"❌ Ошибка проверки соединения с ESP32: {e}")
            return False

    async def connect_esp32(self):
        """Подключение к ESP32"""
        try:
            if self.cli:
                try:
                    await self.cli.disconnect()
                except:
                    pass
                self.cli = None

            self.cli = aioesphomeapi.APIClient(self.esp_host, self.esp_port, self.esp_password)
            await self.cli.connect(login=True)
            logger.info("✅ Подключено к ESP32")

            device_info = await self.cli.device_info()
            logger.info(f"📍 Устройство: {device_info.name}")

            return True
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к ESP32: {e}")
            self.cli = None
            return False

    async def wait_for_device_activation(self):
        """Ожидание активации устройства (нажатия кнопки)"""
        logger.info("⏳ Ожидание активации устройства (нажмите кнопку на ESP32)...")

        async def handle_start(conversation_id: str, flags: int, audio_settings, wake_word_phrase: str | None):
            self.conversation_id = conversation_id
            self.voice_assistant_active = True
            self.device_activated = True
            logger.info(f"🎙️ Устройство активировано: {conversation_id}")
            return 0

        async def handle_stop(expected_stop: bool):
            logger.info("⏹️ Прием аудио с ESP32 остановлен")
            self.voice_assistant_active = False
            self.device_activated = False

        async def handle_audio(audio_data: bytes):
            """Получение аудио с ESP32 и отправка в SIP звонок"""
            if self.voice_assistant_active and len(audio_data) > 0 and self.call_connected:
                # Аудио с ESP32 идет в SIP звонок (собеседник слышит ESP32)
                try:
                    # Помещаем данные в очередь (ожидаем 16-bit PCM с частотой ESP)
                    self.esp_to_sip_queue.put_nowait(audio_data)
                except queue.Full:
                    logger.warning(
                        f"⚠️ Очередь ESP32->SIP переполнена (размер: {self.esp_to_sip_queue.qsize()}). Последний фрейм сброшен.")

        try:
            self.unsubscribe_callback = self.cli.subscribe_voice_assistant(
                handle_start=handle_start,
                handle_stop=handle_stop,
                handle_audio=handle_audio
            )
            logger.info("✅ Ожидание активации устройства...")

            # Ждем активации устройства
            start_time = time.time()
            while not self.device_activated:
                if time.time() - start_time > 60:
                    logger.error("❌ Таймаут ожидания активации устройства")
                    return False
                await asyncio.sleep(0.5)

            logger.info("✅ Устройство активировано!")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка ожидания активации устройства: {e}")
            return False

    def setup_sip(self):
        """Инициализация PJSIP в режиме моста"""
        try:
            self.ep = pj.Endpoint()
            self.ep.libCreate()

            ep_cfg = pj.EpConfig()
            ep_cfg.uaConfig.maxCalls = 2
            ep_cfg.medConfig.sndClockRate = 8000  # 8000 Hz для PCMU

            ep_cfg.medConfig.audioFramePtime = 20  # 20ms frames

            ep_cfg.medConfig.ecOptions = 1  # Echo cancellation
            ep_cfg.medConfig.ecTailLen = 200  # Длина эхоподавления
            ep_cfg.medConfig.quality = 8  # Качество (1-10)
            ep_cfg.medConfig.pTime = 20  # Размер пакета

            # ep_cfg.medConfig.clockRate = 8000 # Не уверен, что нужно

            self.ep.libInit(ep_cfg)

            # Настройка кодеков - приоритет для PCMU/8000
            codec_list = [
                ("PCMU/8000", 255),  # G.711 ulaw - наивысший приоритет
                ("PCMA/8000", 0),  # G.711 alaw - отключаем
                ("GSM/8000", 0),
                ("speex/8000", 0),
                ("speex/16000", 0),
                ("speex/32000", 0),
                ("iLBC/8000", 0),
                ("opus/48000", 0),
            ]
            for codec_name, priority in codec_list:
                try:
                    self.ep.codecSetPriority(codec_name, priority)
                except Exception as e:
                    logger.debug(f"Кодек {codec_name} не найден или не может быть настроен: {e}")

            # Отключаем звуковые устройства
            aud_mgr = self.ep.audDevManager()
            aud_mgr.setNullDev()
            logger.info("🔇 Звуковые устройства отключены (режим моста)")

            # Транспорт
            tp_cfg = pj.TransportConfig()
            tp_cfg.port = 0  # Случайный порт
            self.ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp_cfg)
            logger.info("🚪 UDP транспорт создан")

            self.ep.libStart()
            logger.info("▶️  PJSIP запущен")

            # Аккаунт
            acc_cfg = pj.AccountConfig()
            acc_cfg.idUri = "sip:9000@192.168.128.22:5061"
            acc_cfg.regConfig.registrarUri = "sip:192.168.128.22:5061"
            cred = pj.AuthCredInfo("digest", "asterisk", "9000", 0, "3d12d14b415b5b8b2667820156c0a306")
            acc_cfg.sipConfig.authCreds.append(cred)

            self.acc = pj.Account()
            self.acc.create(acc_cfg)

            logger.info("✅ SIP библиотека инициализирована в режиме моста")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации SIP: {e}")
            return False

    async def make_call(self):
        """Совершение SIP звонка"""
        try:
            logger.info(f"📞 Звонок на {self.sip_target_uri}...")

            # Ждем регистрации
            await asyncio.sleep(3)

            call_prm = pj.CallOpParam()
            call_prm.opt.audioCount = 1
            call_prm.opt.videoCount = 0

            self.call = SIPCall(self.acc, bridge=self)
            self.call.makeCall(self.sip_target_uri, call_prm)

            logger.info("🕐 Ожидание ответа...")
            call_answered = False
            max_wait = 30

            for i in range(max_wait):
                if not self.call:
                    break

                try:
                    call_info = self.call.getInfo()

                    if i % 5 == 0:
                        logger.info(f"📊 Статус: {call_info.stateText}")

                    if call_info.state == pj.PJSIP_INV_STATE_CONFIRMED and not call_answered:
                        call_answered = True
                        logger.info("🎉 СОЕДИНЕНИЕ УСТАНОВЛЕНО!")
                        break

                    elif call_info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                        logger.info("📞 Звонок завершен")
                        break

                except Exception as e:
                    logger.debug(f"Ошибка получения статуса звонка: {e}")

                await asyncio.sleep(1)

            if not call_answered:
                logger.warning("⚠️ Звонок не ответили или не установлен")
                return False

            return True
        except Exception as e:
            logger.error(f"❌ Ошибка совершения звонка: {e}")
            return False

    async def setup_audio_bridge(self, audio_media):
        """Настройка аудио моста для передачи звука с ESP32 в SIP"""
        try:
            self.sip_audio_media = audio_media
            logger.info("🔧 Настройка аудио моста (ESP32 -> SIP)...")

            # Создаем наш пользовательский AudioMediaPort
            # Он будет ресемплировать аудио из очереди ESP32 (16k 16-bit) -> SIP (8k 16-bit) и передавать его
            self.sip_audio_port = SIPAudioMediaPort(self.esp_to_sip_queue, esp_clock_rate=self.esp_clock_rate)

            # Создаем порт с именем и форматом 8kHz 16-bit Mono (для PCMU)
            port_name = "ESP32SIPPort"
            fmt = pj.MediaFormatAudio()
            fmt.type = pj.PJMEDIA_TYPE_AUDIO
            fmt.id = pj.PJMEDIA_FORMAT_L16  # 16-bit Linear PCM (входной формат для PCMU)
            fmt.clockRate = 8000  # 8 kHz (после ресемплинга)
            fmt.channelCount = 1  # Mono
            fmt.bitsPerSample = 16  # 16 bits (входной формат для PCMU)
            fmt.frameTimeUsec = 20000  # 20ms (20000 микросекунд)
            fmt.avgBps = 8000 * 1 * 16  # bits per second: 8000 * 1 * 16 = 128000 bps
            fmt.maxBps = fmt.avgBps

            self.sip_audio_port.createPort(port_name, fmt)
            logger.info(f"🎤 Создан пользовательский аудио порт: {port_name} (8kHz, 16-bit)")

            # --- НОВОЕ: Используем startTransmit2 для установки уровня ---
            # Подключаем наш порт (источник) к аудио медиа звонка (приемник)
            # Это означает, что данные из нашего порта будут передаваться в звонок
            tx_param = pj.AudioMediaTransmitParam()
            # Установим уровень, например, 2.0 (усиление в 2 раза)
            # Попробуйте разные значения: 1.0 (без изменений), 1.5, 2.0, 2.5 и т.д.
            # или 0.5, 0.75 для ослабления.
            volume_boost_factor = 2.5  # Установите желаемое значение
            tx_param.level = volume_boost_factor
            logger.info(f"🔊 Установка уровня передачи ESP32->SIP: {volume_boost_factor}x")

            self.sip_audio_port.startTransmit2(self.sip_audio_media, tx_param)
            # --- КОНЕЦ НОВОГО ---

            logger.info("📤 Аудио поток ESP32 -> SIP звонок установлен")

            self.audio_bridge_setup = True
            logger.info("✅ Аудио мост настроен (ESP32 -> SIP, 8kHz 16-bit после ресемплинга для PCMU)")

        except Exception as e:
            logger.error(f"❌ Ошибка настройки аудио моста: {e}")
            # Попытка закрыть порт в случае ошибки
            if self.sip_audio_port:
                try:
                    # self.sip_audio_port.close() # Убран метод close
                    pass
                except:
                    pass
                self.sip_audio_port = None

    async def on_call_connected(self):
        """Вызывается когда звонок установлен"""
        logger.info("🔗 Звонок установлен, готов к передаче аудио с ESP32 в SIP")
        logger.info("🎤 Говорите в микрофон ESP32 - собеседник должен слышать вас!")

    async def start_bridge(self):
        """Запуск моста ESP32 -> SIP"""
        logger.info("🚀 ЗАПУСК МОСТА ESP32 -> SIP (только передача микрофона)")

        # 1. Подключаемся к ESP32
        if not await self.connect_esp32():
            logger.error("❌ Не удалось подключиться к ESP32")
            return

        # 2. Ждем активации устройства (нажатия кнопки)
        if not await self.wait_for_device_activation():
            logger.error("❌ Устройство не активировано")
            return

        # 3. Инициализируем SIP в режиме моста
        if not self.setup_sip():
            logger.error("❌ Не удалось инициализировать SIP")
            return

        # 4. Совершаем звонок
        if not await self.make_call():
            logger.error("❌ Не удалось установить звонок")
            return

        logger.info("🎉 МОСТ АКТИВИРОВАН!")
        logger.info("🔊 Аудио с ESP32 -> SIP звонок (собеседник слышит ESP32)")

        # Основной цикл
        try:
            while self.call_connected:
                await asyncio.sleep(1)

                # Периодически проверяем соединение с ESP32
                if time.time() % 10 < 1:  # Каждые 10 секунд
                    await self.ensure_esp32_connection()

        except KeyboardInterrupt:
            logger.info("\n🛑 Остановка по запросу пользователя...")

        await self.stop_bridge()

    async def stop_bridge(self):
        """Остановка моста"""
        logger.info("🧹 Завершение работы...")

        # Останавливаем передачу аудио
        if self.sip_audio_port and self.sip_audio_media:
            try:
                self.sip_audio_port.stopTransmit(self.sip_audio_media)
                logger.info("📤 Передача аудио в SIP остановлена")
            except Exception as e:
                logger.error(f"Ошибка остановки передачи аудио: {e}")

        # Закрываем пользовательский аудио порт
        if self.sip_audio_port:
            try:
                # self.sip_audio_port.close() # Убран метод close
                pass
            except Exception as e:
                logger.error(f"Ошибка закрытия аудио порта: {e}")
            self.sip_audio_port = None

        if self.call and hasattr(self.call, 'connected') and self.call.connected:
            try:
                self.call.hangup(pj.CallOpParam())
            except:
                pass

        if self.unsubscribe_callback:
            self.unsubscribe_callback()

        if self.ep:
            try:
                self.ep.libDestroy()
            except:
                pass

        if self.cli:
            try:
                await self.cli.disconnect()
            except:
                pass

        logger.info("👋 Работа завершена")


async def main():
    """Основная функция"""
    ESP_HOST = "192.168.0.103"
    ESP_PORT = 6053
    ESP_PASSWORD = ""
    SIP_TARGET_URI = "sip:539@192.168.128.22:5061"
    # Укажите частоту дискретизации, с которой ESP32 отправляет аудио (обычно 16000)
    ESP_CLOCK_RATE = 16000  # Проверьте вашу конфигурацию ESPHome

    # Проверка зависимостей
    try:
        import aioesphomeapi
        print("✅ aioesphomeapi доступен")
    except ImportError:
        print("❌ aioesphomeapi не установлен")
        exit(1)

    try:
        import pjsua2
        print("✅ pjsua2 доступен")
    except ImportError:
        print("❌ pjsua2 не установлен")
        exit(1)

    try:
        import scipy
        print("✅ scipy доступен (для ресемплинга)")
    except ImportError:
        print("❌ scipy не установлен. Установите с помощью 'pip install scipy'")
        exit(1)

    bridge = ESP32SIPAudioBridge(
        esp_host=ESP_HOST,
        esp_port=ESP_PORT,
        esp_password=ESP_PASSWORD,
        sip_target_uri=SIP_TARGET_URI,
        esp_clock_rate=ESP_CLOCK_RATE  # Передаем частоту ESP
    )

    await bridge.start_bridge()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Программа завершена")