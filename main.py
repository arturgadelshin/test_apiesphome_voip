import pjsua2 as pj
import asyncio
import aioesphomeapi
import pyaudio
import threading
import queue
import logging
import aiohttp
from aiohttp import web
import subprocess
import secrets
from collections import defaultdict
import socket

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class SIPCall(pj.Call):
    """Класс для обработки SIP звонков (из рабочего кода)"""

    def __init__(self, acc, call_id=-1, bridge=None):
        pj.Call.__init__(self, acc, call_id)
        self.bridge = bridge
        self.connected = False

    def onCallState(self, prm):
        ci = self.getInfo()
        logger.info(f"📞 Статус звонка: {ci.stateText}")

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.connected = True
            logger.info("✅ Звонок принят! Разговор начался...")
            if self.bridge:
                self.bridge.call_connected = True
                # Запускаем стриминг на ESP32 при подключении звонка
                asyncio.create_task(self.bridge.start_streaming_to_esp32())

        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.connected = False
            if self.bridge:
                self.bridge.call_connected = False
            logger.info("❌ Звонок завершен")

    def onCallMediaState(self, prm):
        """Callback при изменении состояния медиа потока"""
        logger.info("🎵 Медиа поток активирован")
        if self.connected and self.bridge:
            self.bridge.connect_audio_devices()


class ESP32AudioBridgePJSIP:
    def __init__(self, esp_host, esp_port, esp_password, sip_target_uri):
        # --- ESP32 ---
        self.esp_host = esp_host
        self.esp_port = esp_port
        self.esp_password = esp_password
        self.cli = None
        self.media_player_key = None
        self.voice_assistant_active = False
        self.conversation_id = None
        self.unsubscribe_callback = None

        # --- SIP ---
        self.sip_target_uri = sip_target_uri
        self.ep = None
        self.acc = None
        self.call = None
        self.call_connected = False

        # --- Audio ---
        self.sample_rate = 16000
        self.channels = 1
        self.chunk_size = 512

        # Очереди для аудио данных
        self.esp_to_sip_queue = queue.Queue()  # Аудио с ESP32 -> SIP
        self.sip_to_esp_queue = queue.Queue()  # Аудио из SIP -> ESP32

        # PyAudio для воспроизведения аудио с ESP32
        self.py_audio = None
        self.output_stream = None
        self.is_playing = False
        self.stop_playback_event = threading.Event()
        self.playback_thread = None

        # HTTP сервер для стриминга на ESP32
        self.http_server = None
        self.http_port = 8080
        self.stream_url = None
        self.http_started = False

    async def connect_esp32(self):
        """Подключение к ESP32"""
        try:
            self.cli = aioesphomeapi.APIClient(self.esp_host, self.esp_port, self.esp_password)
            await self.cli.connect(login=True)
            logger.info("✅ Подключено к ESP32")

            device_info = await self.cli.device_info()
            logger.info(f"📍 Устройство: {device_info.name}")

            await self._find_media_player()
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к ESP32: {e}")
            return False

    async def _find_media_player(self):
        """Поиск медиаплеера на ESP32"""
        try:
            entities, services = await self.cli.list_entities_services()
            for entity in entities:
                if hasattr(entity, 'object_id') and 'media_player' in str(entity.object_id).lower():
                    self.media_player_key = entity.key
                    logger.info(f"🎵 Найден медиаплеер: {entity.object_id}")
                    break
            if not self.media_player_key:
                logger.warning("⚠️ Медиаплеер не найден")
        except Exception as e:
            logger.error(f"❌ Ошибка поиска медиаплеера: {e}")

    def start_audio_playback(self):
        """Запуск воспроизведения аудио с ESP32"""
        try:
            self.py_audio = pyaudio.PyAudio()
            self.output_stream = self.py_audio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                frames_per_buffer=self.chunk_size
            )
            self.is_playing = True
            self.stop_playback_event.clear()
            self.playback_thread = threading.Thread(target=self._playback_worker)
            self.playback_thread.daemon = True
            self.playback_thread.start()
            logger.info("🔊 Воспроизведение запущено")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка запуска воспроизведения: {e}")
            return False

    def _playback_worker(self):
        """Рабочий поток для воспроизведения"""
        try:
            while not self.stop_playback_event.is_set():
                try:
                    audio_data = self.esp_to_sip_queue.get(timeout=0.1)
                    if audio_data and self.output_stream:
                        self.output_stream.write(audio_data)
                except queue.Empty:
                    continue
        except Exception as e:
            logger.error(f"❌ Ошибка в рабочем потоке воспроизведения: {e}")

    def stop_audio_playback(self):
        """Остановка воспроизведения"""
        self.is_playing = False
        self.stop_playback_event.set()
        if self.playback_thread:
            self.playback_thread.join(timeout=2.0)
        if self.output_stream:
            self.output_stream.stop_stream()
            self.output_stream.close()
        if self.py_audio:
            self.py_audio.terminate()

    async def start_voice_assistant(self):
        """Активация голосового ассистента для приема аудио с ESP32"""
        logger.info("🎤 Активация приема аудио с ESP32...")

        def handle_start(conversation_id: str, flags: int, audio_settings, wake_word_phrase: str | None):
            self.conversation_id = conversation_id
            self.voice_assistant_active = True
            logger.info(f"🎙️ Прием аудио начат: {conversation_id}")

            # Запускаем воспроизведение при начале разговора
            if not self.is_playing:
                self.start_audio_playback()
            return 0

        def handle_stop(expected_stop: bool):
            logger.info("⏹️ Прием аудио остановлен")
            self.voice_assistant_active = False

        async def handle_audio(audio_data: bytes):
            if self.voice_assistant_active and len(audio_data) > 0:
                # Отправляем аудио в очередь для SIP
                self.esp_to_sip_queue.put(audio_data)

        try:
            self.unsubscribe_callback = self.cli.subscribe_voice_assistant(
                handle_start=handle_start,
                handle_stop=handle_stop,
                handle_audio=handle_audio
            )
            logger.info("✅ Прием аудио с ESP32 активирован")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка активации приема аудио: {e}")
            return False

    # HTTP сервер для стриминга (упрощенная версия)
    async def start_http_server(self):
        """Запуск HTTP сервера для стриминга аудио в ESP32"""
        app = web.Application()

        async def handle_ffmpeg_proxy(request):
            """Обработчик FFmpeg proxy"""
            # Создаем response
            response = web.StreamResponse(
                headers={
                    'Content-Type': 'audio/flac',
                    'Cache-Control': 'no-cache'
                }
            )
            await response.prepare(request)

            # Команда ffmpeg для конвертации
            command_args = [
                "ffmpeg",
                "-f", "s16le", "-ac", "1", "-ar", "16000",
                "-i", "pipe:0",
                "-f", "flac", "-ac", "1", "-ar", "48000",
                "-loglevel", "error",
                "pipe:1"
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *command_args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # Задачи для чтения и записи
                async def write_audio():
                    try:
                        while proc.returncode is None:
                            try:
                                # Берем аудио из очереди SIP->ESP
                                audio_data = await asyncio.get_event_loop().run_in_executor(
                                    None,
                                    lambda: self.sip_to_esp_queue.get(timeout=0.1)
                                )
                                if audio_data:
                                    proc.stdin.write(audio_data)
                                    await proc.stdin.drain()
                            except queue.Empty:
                                await asyncio.sleep(0.01)
                    except Exception as e:
                        logger.debug(f"Ошибка записи в ffmpeg: {e}")

                async def read_output():
                    try:
                        while True:
                            chunk = await proc.stdout.read(4096)
                            if not chunk:
                                break
                            await response.write(chunk)
                    except Exception as e:
                        logger.debug(f"Ошибка чтения из ffmpeg: {e}")

                await asyncio.gather(write_audio(), read_output())

            except Exception as e:
                logger.error(f"❌ Ошибка ffmpeg: {e}")
            finally:
                if proc and proc.returncode is None:
                    proc.terminate()

            return response

        app.router.add_get('/stream.flac', handle_ffmpeg_proxy)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', self.http_port)
        await site.start()

        self.http_server = runner
        local_ip = self._get_local_ip()
        self.stream_url = f"http://{local_ip}:{self.http_port}/stream.flac"
        logger.info(f"🌐 HTTP сервер запущен: {self.stream_url}")
        return True

    def _get_local_ip(self):
        """Получение локального IP"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    async def start_streaming_to_esp32(self):
        """Запуск отправки аудио на ESP32"""
        if not self.media_player_key:
            logger.error("❌ Медиаплеер не найден")
            return False

        try:
            # Устанавливаем URL стрима на медиаплеере
            await self.cli.media_player_command(
                key=self.media_player_key,
                media_url=self.stream_url
            )
            logger.info("✅ Стриминг на ESP32 запущен")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка запуска стриминга на ESP32: {e}")
            return False

    # PJSIP методы (из рабочего кода)
    def setup_sip(self):
        """Инициализация PJSIP"""
        try:
            self.ep = pj.Endpoint()
            self.ep.libCreate()

            # Конфигурация (из рабочего кода)
            ep_cfg = pj.EpConfig()
            ep_cfg.uaConfig.maxCalls = 2
            ep_cfg.medConfig.sndClockRate = 8000  # Частота звука
            ep_cfg.medConfig.audioFramePtime = 20  # Размер фрейма

            self.ep.libInit(ep_cfg)

            # Настройка кодеков (из рабочего кода)
            codec_list = [
                ("PCMU/8000", 255),
                ("PCMA/8000", 254),
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
                    logger.debug(f"Кодек {codec_name} не найден: {e}")

            # Настройка аудиоустройств (из рабочего кода)
            try:
                aud_mgr = self.ep.audDevManager()
                aud_mgr.refreshDevs()

                # Ищем подходящее полнодуплексное устройство
                found_device = False
                dev_count = aud_mgr.getDevCount()
                for i in range(dev_count):
                    dev_info = aud_mgr.getDevInfo(i)
                    if dev_info.inputCount > 0 and dev_info.outputCount > 0:
                        logger.info(f"🎯 Найдено полнодуплексное устройство: {dev_info.name} (ID: {i})")
                        aud_mgr.setCaptureDev(i)
                        aud_mgr.setPlaybackDev(i)
                        found_device = True
                        break

                if not found_device:
                    logger.warning("⚠ Полнофункциональное аудиоустройство не найдено, используем default")
                    aud_mgr.setCaptureDev(-1)
                    aud_mgr.setPlaybackDev(-1)

            except Exception as e:
                logger.error(f"❌ Ошибка настройки аудиоустройств: {e}")
                return False

            # Транспорт
            tp_cfg = pj.TransportConfig()
            tp_cfg.port = 5060
            self.ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp_cfg)

            self.ep.libStart()

            # Аккаунт (из рабочего кода)
            acc_cfg = pj.AccountConfig()
            acc_cfg.idUri = "sip:9000@192.168.128.22:5061"
            acc_cfg.regConfig.registrarUri = "sip:192.168.128.22:5061"
            cred = pj.AuthCredInfo("digest", "asterisk", "9000", 0, "3d12d14b415b5b8b2667820156c0a306")
            acc_cfg.sipConfig.authCreds.append(cred)

            self.acc = pj.Account()
            self.acc.create(acc_cfg)

            logger.info("✅ SIP библиотека инициализирована")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации SIP: {e}")
            return False

    async def make_call(self):
        """Совершение SIP звонка (адаптировано из рабочего кода)"""
        try:
            logger.info(f"📞 Звонок на {self.sip_target_uri}...")

            # Ждем регистрации (из рабочего кода)
            logger.info("⏳ Регистрация...")
            await asyncio.sleep(3)

            call_prm = pj.CallOpParam()
            call_prm.opt.audioCount = 1
            call_prm.opt.videoCount = 0

            # Используем наш класс SIPCall с наследованием
            self.call = SIPCall(self.acc, bridge=self)
            self.call.makeCall(self.sip_target_uri, call_prm)

            # Ожидание ответа (из рабочего кода)
            logger.info("🕐 Ожидание ответа и соединения...")
            call_answered = False
            max_wait = 30

            for i in range(max_wait):
                if not self.call:
                    break

                try:
                    call_info = self.call.getInfo()

                    if i % 5 == 0:
                        logger.info(f"📊 Статус: {call_info.stateText} ({i}с)")

                    if call_info.state == pj.PJSIP_INV_STATE_CONFIRMED and not call_answered:
                        call_answered = True
                        logger.info("🎉 СОЕДИНЕНИЕ УСТАНОВЛЕНО!")
                        break

                    elif call_info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                        logger.info("📞 Звонок завершен удаленной стороной")
                        break

                except Exception as e:
                    logger.debug(f"Ошибка статуса: {e}")

                await asyncio.sleep(1)

            if not call_answered:
                logger.warning("⚠️ Звонок не ответили за 30 секунд")
                return False

            return True
        except Exception as e:
            logger.error(f"❌ Ошибка звонка: {e}")
            return False

    def connect_audio_devices(self):
        """Подключение аудиоустройств к медиа потоку (из рабочего кода)"""
        try:
            # Получаем аудио медиа звонка
            call_aud_med = self.call.getAudioMedia(0)

            # Получаем медиа-порт микрофона (устройство записи)
            aud_mgr = pj.Endpoint.instance().audDevManager()
            mic_med = aud_mgr.getCaptureDevMedia()

            # Подключаем микрофон к медиа потоку (передача вашего голоса)
            mic_med.startTransmit(call_aud_med)
            logger.info("🎤 Микрофон подключен к звонку (ваш голос передаётся)")

            # Подключаем медиа поток к динамикам (воспроизведение голоса собеседника)
            speaker_med = aud_mgr.getPlaybackDevMedia()
            call_aud_med.startTransmit(speaker_med)
            logger.info("🔈 Динамики подключены к звонку (голос собеседника слышен)")

            # Дополнительно: создаем кастомный порт для интеграции с ESP32
            self.setup_esp32_audio_bridge(call_aud_med)

            logger.info("🎉 Реал-таймовый разговор активирован!")
            logger.info("🗣️ ГОВОРИТЕ И СЛУШАЙТЕ!")

        except Exception as e:
            logger.error(f"❌ Ошибка подключения аудиоустройств: {e}")

    def setup_esp32_audio_bridge(self, call_aud_med):
        """Настройка аудиомоста с ESP32"""
        try:
            # Создаем кастомный аудио порт для моста между SIP и ESP32
            self.audio_port = SIPBridgeAudioPort(
                self.esp_to_sip_queue,
                self.sip_to_esp_queue
            )
            self.audio_port.createPort("ESP32Bridge", 8000, 1, 160, 16)

            # Подключаем bidirectional аудио
            call_aud_med.startTransmit(self.audio_port)  # SIP -> ESP32
            self.audio_port.startTransmit(call_aud_med)  # ESP32 -> SIP

            logger.info("✅ Аудио мост SIP-ESP32 активирован")
            logger.info("🔊 Двусторонняя аудио связь установлена")

        except Exception as e:
            logger.error(f"❌ Ошибка настройки аудио моста: {e}")

    async def start_bridge(self):
        """Запуск всего моста"""
        # 1. Подключаемся к ESP32
        if not await self.connect_esp32():
            return

        # 2. Запускаем HTTP сервер для стриминга
        if not await self.start_http_server():
            logger.error("❌ Не удалось запустить HTTP сервер")
            return

        # 3. Активируем прием аудио с ESP32
        await self.start_voice_assistant()

        # 4. Инициализируем SIP
        if not self.setup_sip():
            return

        # 5. Совершаем звонок
        if not await self.make_call():
            return

        logger.info("🎉 МОСТ АКТИВИРОВАН!")
        logger.info("🔊 Аудио с ESP32 -> SIP звонок")
        logger.info("🔊 Аудио из SIP -> ESP32")

        # Основной цикл
        try:
            while self.call_connected:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("🛑 Остановка...")

        await self.stop_bridge()

    async def stop_bridge(self):
        """Остановка моста"""
        logger.info("🧹 Завершение работы...")

        if self.call and hasattr(self.call, 'connected') and self.call.connected:
            try:
                logger.info("📞 Завершаем звонок...")
                self.call.hangup(pj.CallOpParam())
            except:
                pass

        if self.unsubscribe_callback:
            self.unsubscribe_callback()

        self.stop_audio_playback()

        if hasattr(self, 'audio_port') and self.audio_port:
            try:
                self.audio_port.destroyPort()
            except:
                pass

        if self.http_server:
            await self.http_server.cleanup()

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


# Кастомный аудио порт для моста между SIP и ESP32
class SIPBridgeAudioPort(pj.AudioMediaPort):
    def __init__(self, esp_to_sip_queue, sip_to_esp_queue):
        super().__init__()
        self.esp_to_sip_queue = esp_to_sip_queue  # ESP32 -> SIP
        self.sip_to_esp_queue = sip_to_esp_queue  # SIP -> ESP32
        self.frame_size = 160  # 20ms at 8000 Hz

    def onFrameRequested(self, frame):
        """Вызывается когда SIP нужны данные для отправки (берем с ESP32)"""
        try:
            # Берем аудио с ESP32
            audio_data = self.esp_to_sip_queue.get_nowait()
            # Конвертируем 16kHz -> 8kHz если нужно (просто обрезаем)
            if len(audio_data) > self.frame_size * 2:
                audio_data = audio_data[:self.frame_size * 2]
            frame.buf = audio_data
            frame.size = len(audio_data)
        except queue.Empty:
            # Тишина если нет данных
            frame.buf = b'\x00' * (self.frame_size * 2)
            frame.size = self.frame_size * 2

    def onFrameReceived(self, frame):
        """Вызывается когда SIP получает данные (отправляем на ESP32)"""
        if frame.size > 0:
            # Отправляем аудио в очередь для ESP32
            self.sip_to_esp_queue.put(frame.buf[:frame.size])


async def main():
    bridge = ESP32AudioBridgePJSIP(
        esp_host="192.168.0.103",
        esp_port=6053,
        esp_password="",
        sip_target_uri="sip:539@192.168.128.22:5061"
    )

    await bridge.start_bridge()


if __name__ == "__main__":
    asyncio.run(main())