import asyncio
import aioesphomeapi
from aioesphomeapi.api_pb2 import MediaPlayerCommand
import logging
import aiohttp
from aiohttp import web
import threading
import time

# logging.basicConfig(level=logging.DEBUG)

ESP_HOST = "192.168.0.109"
ESP_PORT = 6053
ESP_PASSWORD = ""

LOCAL_FILE_PATH = "audio8.wav"  # Файл в той же директории, что и скрипт
STREAM_PORT = 8989  # Порт для нашего локального стриминг-сервера
STREAM_URL = f"http://192.168.0.106:{STREAM_PORT}/stream.wav" # URL, который будет слушать ESP32

# --- Код стриминг-сервера ---
async def stream_handler(request):
    """Обработчик HTTP-запроса для стриминга файла."""
    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'audio/wav',  # Указываем правильный MIME-тип
            'Access-Control-Allow-Origin': '*',  # Политика CORS (опционально)
        }
    )
    await response.prepare(request)

    try:
        with open(LOCAL_FILE_PATH, 'rb') as f:
            # Читаем и отправляем файл по частям (чанкам)
            while True:
                chunk = f.read(8192)  # Размер чанка 8 КБ
                if not chunk:
                    print("Файл закончился.")
                    break
                await response.write(chunk)
                # await response.drain()  # УБРАНО: deprecated
                # Вместо drain, можно использовать sleep для симуляции реального потока
                await asyncio.sleep(0.01)
        print("Стриминг завершен.")
    except ConnectionResetError:
        print("Клиент (ESP32) закрыл соединение.")
    except Exception as e:
        print(f"Ошибка при стриминге: {e}")

    return response

async def start_stream_server():
    """Запускает aiohttp-сервер для стриминга."""
    app = web.Application()
    app.router.add_get('/stream.wav', stream_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', STREAM_PORT) # Слушаем на всех интерфейсах
    await site.start()
    print(f"Стриминг-сервер запущен на порту {STREAM_PORT}")
    return runner

# --- Код для ESPHome API ---
async def play_audio_stream():
    api_client = None
    media_player_key = None
    server_runner = None

    try:
        # Запускаем стриминг-сервер в asyncio event loop
        server_runner = await start_stream_server()

        # Подключаемся к ESP32
        api_client = aioesphomeapi.APIClient(address=ESP_HOST, port=ESP_PORT, password=ESP_PASSWORD)
        print(f"Подключение к {ESP_HOST}:{ESP_PORT}...")
        await api_client.connect(login=True)
        print("Подключено!")

        device_info = await api_client.device_info()
        print(f"Устройство: {device_info.name}")

        entities, _ = await api_client.list_entities_services()

        for entity in entities:
            if type(entity).__name__ == 'MediaPlayerInfo' and getattr(entity, 'object_id', None) == 'speaker_media_player':
                media_player_key = entity.key
                print(f"Найден медиаплеер: key={media_player_key}")
                break

        if media_player_key is None:
            print("Медиаплеер не найден.")
            return

        print(f"Отправка команды PLAY с URL стрима: {STREAM_URL}")

        api_client.media_player_command(
            key=media_player_key,
            command=MediaPlayerCommand.MEDIA_PLAYER_COMMAND_PLAY,
            media_url=STREAM_URL
        )

        print("Команда отправлена. ESP32 начнет воспроизведение, если сможет подключиться к стриму.")

        # Ждем 30 секунд, чтобы дать ESP32 время на воспроизведение
        await asyncio.sleep(30)

    except Exception as e:
        print(f"Ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Останавливаем сервер
        if server_runner:
            await server_runner.cleanup()
            print("Стриминг-сервер остановлен.")
        # Отключаемся от ESP32
        if api_client:
            await api_client.disconnect()
            print("Отключено от ESP32.")

if __name__ == "__main__":
    asyncio.run(play_audio_stream())