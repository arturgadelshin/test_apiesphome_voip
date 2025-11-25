import aioesphomeapi
import asyncio
import pyaudio
import threading
import queue
import logging
import socket
import secrets
import subprocess
import os
import time
from datetime import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from aiohttp import web

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === НОВЫЙ КЛАСС: Стриминг из файла ===
class FileAudioStreamer:
    """
    Стример аудиофайла (например, piper.mp3) с перекодированием в raw PCM
    """

    def __init__(self, file_path: str, chunk=512):
        self.file_path = file_path
        self.chunk = chunk
        self.is_playing = False
        self.stop_event = threading.Event()
        self.audio_thread = None
        self.active_ffmpeg_processes = set()
        self.audio_data_queue = queue.Queue()

    def start_capture(self):
        """Запуск воспроизведения файла"""
        if not os.path.exists(self.file_path):
            print(f"❌ Файл не найден: {self.file_path}")
            return False

        self.is_playing = True
        self.stop_event.clear()
        self.audio_thread = threading.Thread(target=self._stream_file)
        self.audio_thread.start()
        asyncio.create_task(self._distribute_audio_data())
        print(f"🎵 Стриминг файла: {self.file_path}")
        return True

    def _stream_file(self):
        """Декодируем MP3 → raw s16le 48kHz stereo через ffmpeg"""
        while not self.stop_event.is_set():
            command = [
                "ffmpeg",
                "-i", self.file_path,
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "-ac", "2",
                "-ar", "48000",
                "-vn",  # без видео
                "-loglevel", "error",
                "pipe:1"
            ]

            try:
                proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0
                )

                while not self.stop_event.is_set():
                    data = proc.stdout.read(self.chunk)
                    if not data:
                        break
                    self.audio_data_queue.put(data)

                proc.terminate()
                proc.wait()
                if self.stop_event.is_set():
                    break
                # Пауза перед повтором (опционально)
                time.sleep(0.1)
            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"Ошибка при стриминге файла: {e}")
                break
        print("Файл завершён или остановлен")

    async def _distribute_audio_data(self):
        """Распределение данных во все активные ffmpeg процессы"""
        while self.is_playing:
            try:
                chunk = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.audio_data_queue.get(timeout=0.1)
                )
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
        self.active_ffmpeg_processes.add(convert_info)
        print(f"➕ Добавлен ffmpeg процесс (файл), всего: {len(self.active_ffmpeg_processes)}")

    def remove_ffmpeg_process(self, convert_info):
        if convert_info in self.active_ffmpeg_processes:
            self.active_ffmpeg_processes.discard(convert_info)
            print(f"➖ Удален ffmpeg процесс (файл), осталось: {len(self.active_ffmpeg_processes)}")

    async def stop(self):
        self.is_playing = False
        self.stop_event.set()
        if self.audio_thread:
            self.audio_thread.join(timeout=2.0)
        self.active_ffmpeg_processes.clear()
        print("⏹️ Стриминг файла остановлен")


# === ОСТАЛЬНЫЕ КЛАССЫ БЕЗ ИЗМЕНЕНИЙ (только замена имени стримера) ===

@dataclass
class FFmpegConversionInfo:
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


class LowLatencyAudioStreamServer:
    def __init__(self, host='0.0.0.0', port=8080):
        self.host = host
        self.port = port
        self.app = web.Application()
        self.runner = None
        self.site = None
        self.proxy_data = FFmpegProxyData()
        self.microphone_streamer = None
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get('/api/esphome/ffmpeg_proxy/{device_id}/{filename}', self._handle_ffmpeg_proxy)
        self.app.router.add_get('/health', self._handle_health)

    def set_microphone_streamer(self, streamer):
        self.microphone_streamer = streamer

    async def _handle_ffmpeg_proxy(self, request):
        device_id = request.match_info['device_id']
        filename = request.match_info['filename']

        device_conversions = self.proxy_data.conversions[device_id]
        if not device_conversions:
            return web.Response(text="No proxy URL for device", status=404)

        convert_id, media_format = filename.rsplit(".", 1)

        convert_info = None
        for info in device_conversions:
            if info.convert_id == convert_id and info.media_format == media_format:
                convert_info = info
                break

        if convert_info is None:
            return web.Response(text="Invalid proxy URL", status=400)

        if convert_info.proc and convert_info.proc.returncode is None:
            convert_info.proc.terminate()
            convert_info.proc = None

        response = web.StreamResponse(
            status=200,
            headers={
                'Content-Type': f'audio/{media_format}',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive'
            }
        )
        await response.prepare(request)

        command_args = [
            "ffmpeg",
            "-f", "s16le",
            "-ac", str(convert_info.channels),
            "-ar", str(convert_info.rate),
            "-i", "pipe:0",
            "-f", convert_info.media_format,
            "-ac", str(convert_info.channels),
            "-ar", str(convert_info.rate),
            "-sample_fmt", "s16",
            "-map_metadata", "-1",
            "-vn",
            "-nostats",
            "-loglevel", "error",
            "-fflags", "+nobuffer+flush_packets",
            "-avioflags", "direct",
            "-flags", "low_delay",
            "-threads", "1",
            "-probesize", "32",
            "-analyzeduration", "0",
            "pipe:1"
        ]

        print(f"🚀 Запуск низколатентного ffmpeg: {' '.join(command_args)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *command_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            convert_info.proc = proc
            convert_info.input_stream = proc.stdin

            if self.microphone_streamer:
                self.microphone_streamer.add_ffmpeg_process(convert_info)

            write_task = asyncio.create_task(self._write_audio_to_ffmpeg(convert_info))
            read_task = asyncio.create_task(self._read_ffmpeg_output(proc, response))

            try:
                await asyncio.gather(write_task, read_task)
            except Exception as e:
                print(f"Ошибка в задачах ffmpeg: {e}")

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
        try:
            while (convert_info.proc and
                   convert_info.proc.returncode is None and
                   convert_info.input_stream and
                   not convert_info.input_stream.is_closing()):
                try:
                    chunk = await asyncio.wait_for(convert_info.audio_queue.get(), timeout=1.0)
                    convert_info.input_stream.write(chunk)
                    await convert_info.input_stream.drain()
                except asyncio.TimeoutError:
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
        active_count = len(self.microphone_streamer.active_ffmpeg_processes) if self.microphone_streamer else 0
        return web.json_response({
            'status': 'ok',
            'active_processes': active_count
        })

    def create_proxy_url(self, device_id: str) -> str:
        return self.proxy_data.create_proxy_url(
            device_id=device_id,
            media_format="flac",
            rate=48000,
            channels=2,
            width=2
        )

    def get_stream_url(self, device_id: str) -> str:
        proxy_path = self.create_proxy_url(device_id)
        return f"http://{self.get_local_ip()}:{self.port}{proxy_path}"

    def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        print(f"🌐 HTTP-сервер запущен на порту {self.port}")

    async def stop(self):
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()

        for device_conversions in self.proxy_data.conversions.values():
            for convert_info in device_conversions:
                if convert_info.proc and convert_info.proc.returncode is None:
                    convert_info.proc.terminate()

        print("🔴 HTTP-сервер остановлен")


class ESP32AudioBridge:
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password
        self.cli = None
        self.is_connected = False

        # Приём с ESP32
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

        # ОТПРАВКА: используем ФАЙЛ вместо микрофона
        self.microphone_streamer = FileAudioStreamer(file_path="piper.mp3", chunk=512)

        self.http_server = LowLatencyAudioStreamServer(port=8080)
        self.http_server.set_microphone_streamer(self.microphone_streamer)
        self.http_started = False

        # Медиаплеер ESP32
        self.media_player_key = None
        self.stream_url = None
        self.is_streaming_to_esp32 = False

        # Голосовой ассистент
        self.conversation_id = None
        self.unsubscribe_callback = None
        self.voice_assistant_active = False

    async def connect(self):
        try:
            self.cli = aioesphomeapi.APIClient(self.host, self.port, self.password)
            await self.cli.connect(login=True)
            self.is_connected = True
            device_info = await self.cli.device_info()
            print(f"✅ Подключено к: {device_info.name} (версия: {device_info.esphome_version})")
            await self._find_media_player()
            return True
        except Exception as e:
            print(f"❌ Ошибка подключения: {e}")
            return False

    async def _find_media_player(self):
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

    # === Приём с ESP32 (без изменений) ===
    def start_audio_playback(self):
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
        if self.is_playing:
            self.audio_queue.put(audio_data)

    def stop_audio_playback(self):
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

    # === Отправка на ESP32 (без изменений в логике) ===
    async def start_streaming_to_esp32(self):
        print("\n🚀 ЗАПУСК ОТПРАВКИ АУДИО НА ESP32")

        if not self.media_player_key:
            print("❌ Медиаплеер не найден")
            return False

        if not self.http_started:
            print("🌐 Запуск HTTP сервера...")
            await self.http_server.start()
            self.http_started = True

        self.stream_url = self.http_server.get_stream_url("file_source")
        print(f"🎤 URL стрима для ESP32: {self.stream_url}")

        print("🎵 Запуск стриминга файла...")
        if not self.microphone_streamer.start_capture():
            print("❌ Не удалось запустить стриминг файла")
            return False

        await asyncio.sleep(1)

        success = await self._play_stream_on_esp32()
        if success:
            self.is_streaming_to_esp32 = True
            print("🎉 ОТПРАВКА АУДИО НА ESP32 ЗАПУЩЕНА!")
            print("🔊 ESP32 будет проигрывать содержимое файла piper.mp3")
            return True
        return False

    async def _play_stream_on_esp32(self):
        try:
            self.cli.media_player_command(
                key=self.media_player_key,
                command=aioesphomeapi.MediaPlayerCommand.STOP
            )
            await asyncio.sleep(0.5)

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

    # === Голосовой ассистент (без изменений) ===
    def start_voice_assistant(self):
        print("🎤 Активация приема аудио с ESP32...")

        async def handle_start(conversation_id: str, flags: int, audio_settings, wake_word_phrase: str | None):
            self.conversation_id = conversation_id
            self.voice_assistant_active = True
            print(f"🎙️  Прием аудио с ESP32 начат: {conversation_id}")
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
        print("\n🎯 АВТОМАТИЧЕСКИЙ РЕЖИМ АКТИВИРОВАН!")
        if not self.http_started:
            await self.http_server.start()
            self.http_started = True
            self.stream_url = self.http_server.get_stream_url("file_source")
            print(f"🔗 FLAC поток готов: {self.stream_url}")

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
    HOST = "192.168.0.103"  # ← Замените на IP вашего ESP32
    PORT = 6053
    PASSWORD = ""           # ← Если задан в ESPHome

    print("🚀 ESP32 Audio Bridge — Стриминг файла piper.mp3")
    print("=" * 55)

    bridge = ESP32AudioBridge(HOST, PORT, PASSWORD)

    try:
        if not await bridge.connect():
            return

        await bridge.start_automatic_mode()

        print("\n🎧 СИСТЕМА АКТИВИРОВАНА!")
        print("   Статус потоков:")
        print("   🔊 Прием: ESP32 → Ноутбук     ⚠️  ОЖИДАНИЕ АКТИВАЦИИ")
        print("   🎵 Отправка: piper.mp3 → ESP32 ⚠️  ОЖИДАНИЕ ПРИЕМА")
        print("\n   Инструкция:")
        print("   1. Скажите wake word или нажмите кнопку на ESP32")
        print("   2. ESP32 начнёт проигрывать piper.mp3 и отправлять своё аудио вам")

        try:
            while True:
                await asyncio.sleep(1)
                if bridge.voice_assistant_active:
                    print("🔊 Прием с ESP32: АКТИВЕН | 🎵 Отправка файла: АКТИВНА", end='\r')
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
    deps_ok = True
    try:
        import pyaudio
        print("✅ PyAudio доступен")
    except ImportError:
        print("❌ PyAudio не установлен. Установите: pip install pyaudio")
        deps_ok = False

    try:
        import aioesphomeapi
        print("✅ aioesphomeapi доступен")
    except ImportError:
        print("❌ aioesphomeapi не установлен. Установите: pip install aioesphomeapi")
        deps_ok = False

    try:
        import aiohttp
        print("✅ aiohttp доступен")
    except ImportError:
        print("❌ aiohttp не установлен. Установите: pip install aiohttp")
        deps_ok = False

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        print("✅ FFmpeg доступен")
    except:
        print("❌ FFmpeg не установлен! Установите: sudo apt install ffmpeg")
        deps_ok = False

    if not deps_ok:
        exit(1)

    if not os.path.exists("piper.mp3"):
        print("⚠️  Файл piper.mp3 не найден в текущей директории!")
        print("    Положите файл piper.mp3 рядом со скриптом.")
        exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Программа завершена")