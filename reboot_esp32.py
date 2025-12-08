import aioesphomeapi
import asyncio


async def reboot():
    api = aioesphomeapi.APIClient("192.168.0.121", 6053, "")
    await api.connect(login=True)

    print("Подключено. Отправка команды перезагрузки...")
    api.button_command(1677765501)  # restart_module

    await asyncio.sleep(1)
    await api.disconnect()
    print("✅ Команда отправлена. Устройство должно перезагрузиться.")


if __name__ == "__main__":
    asyncio.run(reboot())