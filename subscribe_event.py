import aioesphomeapi
import asyncio

async def main():
    """Connect to an ESPHome device and wait for state changes."""
    cli = aioesphomeapi.APIClient("192.168.0.103", 6053, "")

    await cli.connect(login=True)
    def change_callback(state):
        """Print the state changes of the device.."""
        print(state)

    # Subscribe to the state changes
    cli.subscribe_states(change_callback)

loop = asyncio.get_event_loop()
try:
    asyncio.ensure_future(main())
    loop.run_forever()
except KeyboardInterrupt:
    pass
finally:
    loop.close()