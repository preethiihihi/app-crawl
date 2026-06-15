import asyncio
import json
import logging
from appium_service import AppiumMcpClient, ensure_appium_server
from explore_with_ollama import get_compressed_page_source, filter_xml_to_elements

logging.basicConfig(level=logging.INFO)

async def main():
    ensure_appium_server(device_type="local emulator", port=4723)
    client = AppiumMcpClient(server_dir="/Users/preethichitte/Documents/mcp_appium_server")
    await client.start()
    try:
        session_args = {
            "deviceName": "Android Emulator",
            "udid": "emulator-5554",
            "appPackage": "com.kuberproject",
            "appActivity": ".MainActivity",
            "deviceType": "local emulator"
        }
        logger = logging.getLogger("inspect_screen")
        logger.info("Starting session...")
        await client.call_tool("start_session", session_args)
        await asyncio.sleep(4.0)
        
        logger.info("Fetching layout via get_compressed_page_source...")
        xml_text = await get_compressed_page_source(client)
        
        logger.info(f"Page Source length retrieved: {len(xml_text)}")
        if xml_text:
            print("--- XML PREVIEW START ---")
            print(xml_text[:1000])
            print("--- XML PREVIEW END ---")
            
            elements = filter_xml_to_elements(xml_text)
            print(f"Discovered elements count: {len(elements)}")
            for idx, el in enumerate(elements[:15]):
                print(f"Element {idx}: {el}")
        else:
            logger.error("Failed to retrieve page source!")
            
    finally:
        await client.stop()

if __name__ == "__main__":
    asyncio.run(main())
