import aioesphomeapi
import asyncio
import pyaudio
import threading
import queue
import logging
import wave
from datetime import datetime
import uuid
import aiohttp
from aiohttp import web
import socket
import secrets
from collections import defaultdict
import subprocess
from dataclasses import dataclass, field
from typing import Optional
import time

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class FFmpegConversionInfo:
    """Информация о конвертации ffmpeg"""

    convert_id: str
    media_format: str = "flac"
    rate: Optional[int] = 48000
    channels: Optional[int] = 2
    width: Optional[int] = 2

    proc: Optional[asyncio.subprocess.Process] = None
    is_finished: bool = False
    input_stream: Optional[asyncio.StreamWriter] = None
    audio_queue: Optional[asyncio.Queue] = None

    def __hash__(self):
        return hash(self.convert_id)

    def __eq__(self, other):
        if not isinstance(other, FFmpegConversionInfo):
            return False
        return self.convert_id == other.convert_id


@dataclass
class FFmpegProxyData:
    """Данные для ffmpeg proxy"""

    conversions: dict[str, list[FFmpegConversionInfo]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def create_proxy_url(
            self,
            device_id: str,
            media_format: str = "flac",
            rate: Optional[int] = 48000,
            channels: Optional[int] = 2,
            width: Optional[int] = 2,
    ) -> str:
        """Создание proxy URL"""

        device_conversions = [
            info for info in self.conversions[device_id] if not info.is_finished
        ]

        while len(device_conversions) >= 2:
            convert_info = device_conversions[0]
            if convert_info.proc and convert_info.proc.returncode is None:
                logger.debug("Останавливаем существующий ffmpeg процесс")
                convert_info.proc.terminate()
            device_conversions = device_conversions[1:]

        convert_id = secrets.token_urlsafe(16)
        convert_info = FFmpegConversionInfo(
            convert_id, media_format, rate, channels, width
        )
        convert_info.audio_queue = asyncio.Queue()

        device_conversions.append(convert_info)
        self.conversions[device_id] = device_conversions

        return f"/api/esphome/ffmpeg_proxy/{device_id}/{convert_id}.{media_format}"


class SimpleMicrophoneStreamer:
    """
    ПРОСТОЙ И ЭФФЕКТИВНЫЙ стример микрофона
    """

    def __init__(self, format=pyaudio.paInt16, channels=2, rate=48000, chunk=512):
        self.format = format
        self.channels = channels
        self.rate = rate
        self.chunk = chunk

        self.audio_interface = None
        self.stream = None
        self.is_recording = False

        self.stop_event = threading.Event()
        self.audio_thread = None

        # Для распределения аудио данных
        self.active_ffmpeg_processes = set()
        self.audio_data_queue = queue.Queue()

    def start_capture(self):
        """Запуск захвата аудио с микрофона"""
        try:
            self.audio_interface = pyaudio.PyAudio()

            self.stream = self.audio_interface.open(
                format=self.format,
                channels=self.channels,
                rate=self.rate,
                input=True,
                frames_per_buffer=self.chunk
            )

            self.is_recording = True
            self.stop_event.clear()

            # Запускаем поток для захвата аудио
            self.audio_thread = threading.Thread(target=self._stream_audio)
            self.audio_thread.start()

            # Запускаем асинхронную задачу для распределения данных
            asyncio.create_task(self._distribute_audio_data())

            print(f"🎤 Стриминг запущен: {self.channels} канал(а), {self.rate} Hz, чанк: {self.chunk}")
            return True

        except Exception as e:
            print(f"❌ Ошибка запуска захвата аудио: {e}")
            return False

    def _stream_audio(self):
        """Внутренний метод для непрерывного стриминга аудио с микрофона"""
        try:
            while not self.stop_event.is_set():
                data = self.stream.read(self.chunk, exception_on_overflow=False)
                # Помещаем данные в очередь для распределения
                self.audio_data_queue.put(data)
        except Exception as e:
            if not self.stop_event.is_set():
                print(f"Ошибка во время стриминга аудио: {e}")
        finally:
            print("Аудио стриминг остановлен")

    async def _distribute_audio_data(self):
        """Асинхронная задача для распределения аудиоданных в ffmpeg процессы"""
        while self.is_recording:
            try:
                # Получаем данные из синхронной очереди
                chunk = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.audio_data_queue.get(timeout=0.1)
                )

                # Распределяем данные во все активные ffmpeg процессы
                for convert_info in list(self.active_ffmpeg_processes):
                    if (convert_info.proc and
                            convert_info.proc.returncode is None and
                            convert_info.audio_queue is not None):
                        try:
                            await convert_info.audio_queue.put(chunk)
                        except Exception as e:
                            print(f"Ошибка отправки в очередь ffmpeg: {e}")
                            self.active_ffmpeg_processes.discard(convert_info)
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Ошибка в распределении аудио: {e}")
                await asyncio.sleep(0.01)

    def add_ffmpeg_process(self, convert_info):
        """Добавление ffmpeg процесса для получения аудио данных"""
        self.active_ffmpeg_processes.add(convert_info)
        print(f"➕ Добавлен ffmpeg процесс, всего: {len(self.active_ffmpeg_processes)}")

    def remove_ffmpeg_process(self, convert_info):
        """Удаление ffmpeg процесса"""
        if convert_info in self.active_ffmpeg_processes:
            self.active_ffmpeg_processes.discard(convert_info)
            print(f"➖ Удален ffmpeg процесс, осталось: {len(self.active_ffmpeg_processes)}")

    async def stop(self):
        """Остановка захвата аудио"""
        self.is_recording = False
        self.stop_event.set()

        if self.audio_thread:
            self.audio_thread.join(timeout=2.0)

        if self.stream:
            self.stream.stop_stream()
            self.stream.close()

        if self.audio_interface:
            self.audio_interface.terminate()
            self.audio_interface = None

        self.active_ffmpeg_processes.clear()
        print("🔇 Стриминг микрофона остановлен")


class LowLatencyAudioStreamServer:
    """Сервер с низкой задержкой"""

    def __init__(self, host='0.0.0.0', port=8080):
        self.host = host
        self.port = port
        self.app = web.Application()
        self.runner = None
        self.site = None
        self.proxy_data = FFmpegProxyData()
        self.microphone_streamer = None

        # Настраиваем маршруты
        self._setup_routes()

    def _setup_routes(self):
        """Настройка всех маршрутов"""
        self.app.router.add_get('/api/esphome/ffmpeg_proxy/{device_id}/{filename}', self._handle_ffmpeg_proxy)
        self.app.router.add_get('/health', self._handle_health)

    def set_microphone_streamer(self, streamer: SimpleMicrophoneStreamer):
        """Установка микрофонного стримера"""
        self.microphone_streamer = streamer

    async def _handle_ffmpeg_proxy(self, request):
        """Обработка ffmpeg proxy запросов с низкой задержкой"""
        device_id = request.match_info['device_id']
        filename = request.match_info['filename']

        device_conversions = self.proxy_data.conversions[device_id]
        if not device_conversions:
            return web.Response(text="No proxy URL for device", status=404)

        # Извлекаем convert_id и формат из filename
        convert_id, media_format = filename.rsplit(".", 1)

        # Ищем информацию о конвертации
        convert_info = None
        for info in device_conversions:
            if info.convert_id == convert_id and info.media_format == media_format:
                convert_info = info
                break

        if convert_info is None:
            return web.Response(text="Invalid proxy URL", status=400)

        # Останавливаем предыдущий процесс если URL переиспользуется
        if convert_info.proc and convert_info.proc.returncode is None:
            convert_info.proc.terminate()
            convert_info.proc = None

        # Создаем response
        response = web.StreamResponse(
            status=200,
            headers={
                'Content-Type': f'audio/{media_format}',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive'
            }
        )
        await response.prepare(request)

        # Запускаем ffmpeg процесс с настройками для минимальной задержки
        command_args = [
            "ffmpeg",
            "-f", "s16le",  # RAW signed 16-bit little-endian
            "-ac", str(convert_info.channels),
            "-ar", str(convert_info.rate),
            "-i", "pipe:0",  # Читаем из stdin
            "-f", convert_info.media_format,
            "-ac", str(convert_info.channels),
            "-ar", str(convert_info.rate),
            "-sample_fmt", "s16",
            "-map_metadata", "-1",
            "-vn",
            "-nostats",
            "-loglevel", "error",
            # Критически важные параметры для низкой задержки:
            "-fflags", "+nobuffer+flush_packets",
            "-avioflags", "direct",
            "-flags", "low_delay",
            "-threads", "1",  # Один поток для меньшей задержки
            "-probesize", "32",  # Минимальный размер анализа
            "-analyzeduration", "0",  # Без анализа длительности
            "pipe:1"  # Пишем в stdout
        ]

        print(f"🚀 Запуск низколатентного ffmpeg: {' '.join(command_args)}")

        try:
            # Создаем процесс с пайпами
            proc = await asyncio.create_subprocess_exec(
                *command_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            convert_info.proc = proc
            convert_info.input_stream = proc.stdin

            # Регистрируем процесс для получения аудио данных
            if self.microphone_streamer:
                self.microphone_streamer.add_ffmpeg_process(convert_info)

            # Запускаем задачи для записи и чтения
            write_task = asyncio.create_task(self._write_audio_to_ffmpeg(convert_info))
            read_task = asyncio.create_task(self._read_ffmpeg_output(proc, response))

            try:
                # Ждем завершения чтения или ошибки
                await asyncio.gather(write_task, read_task)
            except Exception as e:
                print(f"Ошибка в задачах ffmpeg: {e}")

            # Отменяем задачи если они еще работают
            for task in [write_task, read_task]:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            print(f"❌ Ошибка запуска ffmpeg: {e}")
            return web.Response(text="FFmpeg error", status=500)
        finally:
            convert_info.is_finished = True
            if self.microphone_streamer:
                self.microphone_streamer.remove_ffmpeg_process(convert_info)
            if proc and proc.returncode is None:
                proc.terminate()
                if proc.stdin:
                    proc.stdin.close()

        return response

    async def _write_audio_to_ffmpeg(self, convert_info):
        """Асинхронная задача для записи аудиоданных в ffmpeg из очереди"""
        try:
            while (convert_info.proc and
                   convert_info.proc.returncode is None and
                   convert_info.input_stream and
                   not convert_info.input_stream.is_closing()):
                try:
                    # Получаем данные из асинхронной очереди
                    chunk = await asyncio.wait_for(convert_info.audio_queue.get(), timeout=1.0)

                    # Пишем данные в stdin ffmpeg
                    convert_info.input_stream.write(chunk)
                    # Ждем пока данные будут отправлены
                    await convert_info.input_stream.drain()

                except asyncio.TimeoutError:
                    # Если очередь пуста, продолжаем ждать
                    continue
                except (BrokenPipeError, ConnectionResetError):
                    print("🔌 Pipe закрыт, прекращаем запись")
                    break
                except Exception as e:
                    print(f"❌ Ошибка записи в ffmpeg: {e}")
                    break
        except asyncio.CancelledError:
            pass

    async def _read_ffmpeg_output(self, proc, response):
        """Чтение вывода ffmpeg и отправка клиенту"""
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                try:
                    await response.write(chunk)
                except (ConnectionResetError, ConnectionError):
                    print("🔌 Клиент отключился")
                    break
        except Exception as e:
            print(f"❌ Ошибка чтения из ffmpeg: {e}")

    async def _handle_health(self, request):
        """Health check endpoint"""
        active_count = len(self.microphone_streamer.active_ffmpeg_processes) if self.microphone_streamer else 0
        return web.json_response({
            'status': 'ok',
            'active_processes': active_count
        })

    def create_proxy_url(self, device_id: str) -> str:
        """Создание proxy URL для устройства"""
        return self.proxy_data.create_proxy_url(
            device_id=device_id,
            media_format="flac",
            rate=48000,
            channels=2,
            width=2
        )

    def get_stream_url(self, device_id: str) -> str:
        """Получение полного URL потока"""
        proxy_path = self.create_proxy_url(device_id)
        return f"http://{self.get_local_ip()}:{self.port}{proxy_path}"

    def get_local_ip(self):
        """Получение локального IP адреса"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    async def start(self):
        """Запуск HTTP-сервера"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        print(f"🌐 HTTP-сервер запущен на порту {self.port}")

    async def stop(self):
        """Остановка HTTP-сервера"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()

        # Останавливаем все ffmpeg процессы
        for device_conversions in self.proxy_data.conversions.values():
            for convert_info in device_conversions:
                if convert_info.proc and convert_info.proc.returncode is None:
                    convert_info.proc.terminate()

        print("🔴 HTTP-сервер остановлен")


class ESP32AudioBridge:
    """Главный класс для двустороннего аудиомоста"""

    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password
        self.cli = None
        self.is_connected = False

        # Для приема аудио с ESP32
        self.sample_rate = 16000
        self.channels = 1
        self.sample_width = 2
        self.chunk_size = 1024

        self.audio_interface = None
        self.output_stream = None
        self.is_playing = False
        self.audio_queue = queue.Queue()
        self.playback_thread = None
        self.stop_playback_event = threading.Event()

        # Для отправки аудио на ESP32
        self.microphone_streamer = SimpleMicrophoneStreamer(
            format=pyaudio.paInt16,
            channels=2,
            rate=48000,
            chunk=512
        )
        self.http_server = LowLatencyAudioStreamServer(port=8080)
        self.http_server.set_microphone_streamer(self.microphone_streamer)
        self.http_started = False

        # Медиаплеер ESP32
        self.media_player_key = None
        self.stream_url = None
        self.is_streaming_to_esp32 = False

        # Для голосового ассистента
        self.conversation_id = None
        self.unsubscribe_callback = None
        self.voice_assistant_active = False

    async def connect(self):
        """Подключение к ESP32"""
        try:
            self.cli = aioesphomeapi.APIClient(self.host, self.port, self.password)
            await self.cli.connect(login=True)
            self.is_connected = True

            device_info = await self.cli.device_info()
            print(f"✅ Подключено к: {device_info.name} (версия: {device_info.esphome_version})")

            # Ищем медиаплеер
            await self._find_media_player()

            return True

        except Exception as e:
            print(f"❌ Ошибка подключения: {e}")
            return False

    async def _find_media_player(self):
        """Поиск медиаплеера на ESP32"""
        try:
            entities, services = await self.cli.list_entities_services()
            for entity in entities:
                if hasattr(entity, 'object_id') and 'media_player' in str(entity.object_id).lower():
                    self.media_player_key = entity.key
                    print(f"🎵 Найден медиаплеер: {entity.name}")
                    break

            if not self.media_player_key:
                print("⚠️ Медиаплеер не найден")

        except Exception as e:
            print(f"❌ Ошибка поиска медиаплеера: {e}")

    # Методы для приема аудио с ESP32
    def start_audio_playback(self):
        """Запуск воспроизведения аудио с ESP32"""
        try:
            self.audio_interface = pyaudio.PyAudio()
            self.output_stream = self.audio_interface.open(
                format=self.audio_interface.get_format_from_width(self.sample_width),
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
            print(f"🔊 Воспроизведение с ESP32 запущено")
            return True
        except Exception as e:
            print(f"❌ Ошибка запуска воспроизведения: {e}")
            return False

    def _playback_worker(self):
        """Рабочий поток для воспроизведения"""
        playback_counter = 0
        try:
            while not self.stop_playback_event.is_set():
                try:
                    audio_data = self.audio_queue.get(timeout=0.1)
                    if audio_data and self.output_stream:
                        self.output_stream.write(audio_data)
                        playback_counter += 1
                        if playback_counter % 50 == 0:
                            print(f"🔊 Воспроизведено чанков: {playback_counter}")
                except queue.Empty:
                    continue
                except Exception as e:
                    print(f"❌ Ошибка воспроизведения: {e}")
                    break
        except Exception as e:
            print(f"❌ Ошибка в рабочем потоке: {e}")

    def add_audio_data(self, audio_data):
        """Добавление аудио данных с ESP32"""
        if self.is_playing:
            self.audio_queue.put(audio_data)

    def stop_audio_playback(self):
        """Остановка воспроизведения"""
        self.is_playing = False
        self.stop_playback_event.set()
        if self.playback_thread:
            self.playback_thread.join(timeout=2.0)
        if self.output_stream:
            try:
                self.output_stream.stop_stream()
                self.output_stream.close()
            except:
                pass
        if self.audio_interface:
            try:
                self.audio_interface.terminate()
            except:
                pass

    # Методы для отправки аудио на ESP32
    async def start_streaming_to_esp32(self):
        """Запуск отправки аудио на ESP32"""
        print("\n🚀 ЗАПУСК ОТПРАВКИ АУДИО НА ESP32")

        if not self.media_player_key:
            print("❌ Медиаплеер не найден")
            return False

        # Запускаем HTTP сервер если не запущен
        if not self.http_started:
            print("🌐 Запуск HTTP сервера...")
            await self.http_server.start()
            self.http_started = True

        # Получаем URL для стрима
        self.stream_url = self.http_server.get_stream_url("laptop_microphone")
        print(f"🎤 URL стрима для ESP32: {self.stream_url}")

        # ЗАПУСКАЕМ МИКРОФОН
        print("🎤 Запуск микрофона...")
        if not self.microphone_streamer.start_capture():
            print("❌ Не удалось запустить микрофон")
            return False

        # Ждем немного чтобы микрофон начал работать
        print("⏳ Ожидание инициализации микрофона...")
        await asyncio.sleep(1)

        # Отправляем URL на медиаплеер ESP32
        success = await self._play_stream_on_esp32()

        if success:
            self.is_streaming_to_esp32 = True
            print("🎉 ОТПРАВКА АУДИО НА ESP32 ЗАПУЩЕНА!")
            print("🎙️  Говорите в микрофон ноутбука - звук пойдет на ESP32")
            return True

        return False

    async def _play_stream_on_esp32(self):
        """Воспроизведение потока на ESP32"""
        try:
            print("🔄 Запуск воспроизведения на ESP32...")

            # Останавливаем предыдущее воспроизведение
            self.cli.media_player_command(
                key=self.media_player_key,
                command=aioesphomeapi.MediaPlayerCommand.STOP
            )

            # Даем время на остановку
            await asyncio.sleep(0.5)

            # Устанавливаем URL
            self.cli.media_player_command(
                key=self.media_player_key,
                media_url=self.stream_url
            )

            print("✅ Воспроизведение запущено на ESP32")
            return True

        except Exception as e:
            print(f"❌ Ошибка запуска воспроизведения на ESP32: {e}")
            return False

    async def stop_streaming_to_esp32(self):
        """Остановка отправки аудио на ESP32"""
        if self.is_streaming_to_esp32:
            print("🛑 Остановка отправки аудио на ESP32...")
            await self.microphone_streamer.stop()

            if self.media_player_key:
                try:
                    self.cli.media_player_command(
                        key=self.media_player_key,
                        command=aioesphomeapi.MediaPlayerCommand.STOP
                    )
                except Exception as e:
                    print(f"⚠️ Ошибка остановки воспроизведения: {e}")

            self.is_streaming_to_esp32 = False
            print("✅ Отправка аудио на ESP32 остановлена")

    # Методы голосового ассистента (для приема)
    def start_voice_assistant(self):
        """Запуск приема аудио с ESP32"""
        print("🎤 Активация приема аудио с ESP32...")

        async def handle_start(conversation_id: str, flags: int, audio_settings, wake_word_phrase: str | None):
            self.conversation_id = conversation_id
            self.voice_assistant_active = True
            print(f"🎙️  Прием аудио с ESP32 начат: {conversation_id}")

            # ЗАПУСКАЕМ ОТПРАВКУ АУДИО НА ESP32 ОДНОВРЕМЕННО С НАЧАЛОМ ПРИЕМА
            print("🔄 Одновременный запуск отправки аудио на ESP32...")
            asyncio.create_task(self.start_streaming_to_esp32())

            if not self.is_playing:
                self.start_audio_playback()
            return 0

        async def handle_stop(expected_stop: bool):
            print(f"⏹️  Прием аудио с ESP32 остановлен")
            self.voice_assistant_active = False

        async def handle_audio(audio_data: bytes):
            if self.is_playing and self.voice_assistant_active:
                self.add_audio_data(audio_data)
            if len(audio_data) > 0 and self.voice_assistant_active:
                print(f"📥 Аудио с ESP32: {len(audio_data)} байт", end='\r')

        try:
            self.unsubscribe_callback = self.cli.subscribe_voice_assistant(
                handle_start=handle_start,
                handle_stop=handle_stop,
                handle_audio=handle_audio
            )
            print("✅ Прием аудио с ESP32 активирован")
            return True
        except Exception as e:
            print(f"❌ Ошибка активации приема аудио: {e}")
            return False

    async def start_automatic_mode(self):
        """Запуск автоматического режима - ТОЛЬКО ПРИЕМ"""
        print("\n🎯 АВТОМАТИЧЕСКИЙ РЕЖИМ АКТИВИРОВАН!")

        # Запускаем HTTP сервер заранее
        if not self.http_started:
            await self.http_server.start()
            self.http_started = True
            self.stream_url = self.http_server.get_stream_url("laptop_microphone")
            print(f"🔗 FLAC поток готов: {self.stream_url}")

        # Запускаем прием аудио с ESP32
        voice_assistant_started = self.start_voice_assistant()

        if not voice_assistant_started:
            print("⚠️  Не удалось активировать прием аудио с ESP32")
            print("ℹ️   Для приема аудио с ESP32 скажите wake word на устройстве")
        else:
            print("✅ Прием аудио с ESP32 готов")

        print("\n⏳ Ожидание активации голосового ассистента на ESP32...")
        print("   Нажмите кнопку или скажите wake word на ESP32")

        return True

    async def disconnect(self):
        """Отключение"""
        if self.unsubscribe_callback:
            self.unsubscribe_callback()

        self.stop_audio_playback()
        await self.stop_streaming_to_esp32()

        if self.http_started:
            await self.http_server.stop()

        if self.cli and self.is_connected:
            await self.cli.disconnect()
            print("🔌 Отключено от устройства")


async def main():
    """Основная функция"""
    HOST = "192.168.0.103"
    PORT = 6053
    PASSWORD = ""

    print("🚀 ESP32 Audio Bridge - ОДНОВРЕМЕННЫЙ СТАРТ ПРИЕМА И ОТПРАВКИ")
    print("=" * 55)

    bridge = ESP32AudioBridge(HOST, PORT, PASSWORD)

    try:
        if not await bridge.connect():
            return

        # Запускаем автоматический режим
        await bridge.start_automatic_mode()

        print("\n🎧 СИСТЕМА АКТИВИРОВАНА!")
        print("   Статус потоков:")
        print("   🔊 Прием: ESP32 → Ноутбук     ⚠️  ОЖИДАНИЕ АКТИВАЦИИ")
        print("   🎤 Отправка: Ноутбук → ESP32 ⚠️  ОЖИДАНИЕ ПРИЕМА")
        print("\n   Инструкция:")
        print("   1. Скажите wake word или нажмите кнопку на ESP32")
        print("   2. ОДНОВРЕМЕННО запустится:")
        print("      - Прием аудио с ESP32 на ноутбук")
        print("      - Отправка аудио с ноутбука на ESP32")
        print("   3. Для остановки нажмите Ctrl+C")

        # Основной цикл
        try:
            while True:
                await asyncio.sleep(1)

                # Показываем статус
                if bridge.voice_assistant_active:
                    print("🔊 Прием с ESP32: АКТИВЕН | 🎤 Отправка на ESP32: АКТИВНА", end='\r')
                else:
                    print("⏳ Ожидание активации...", end='\r')

        except KeyboardInterrupt:
            print("\n\n🛑 Остановка по запросу пользователя...")

    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🧹 Завершение работы...")
        await bridge.disconnect()
        print("👋 Работа завершена")


if __name__ == "__main__":
    # Проверка зависимостей
    try:
        import pyaudio

        print("✅ PyAudio доступен")
    except ImportError:
        print("❌ PyAudio не установлен. Установите: pip install pyaudio")
        exit(1)

    try:
        import aioesphomeapi

        print("✅ aioesphomeapi доступен")
    except ImportError:
        print("❌ aioesphomeapi не установлен. Установите: pip install aioesphomeapi")
        exit(1)

    try:
        import aiohttp

        print("✅ aiohttp доступен")
    except ImportError:
        print("❌ aiohttp не установлен. Установите: pip install aiohttp")
        exit(1)

    # Проверка ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        print("✅ FFmpeg доступен")
    except:
        print("❌ FFmpeg не установлен! Установите: sudo apt install ffmpeg")
        exit(1)

    # Запуск
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Программa завершена")