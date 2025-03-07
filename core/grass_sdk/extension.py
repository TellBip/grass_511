import asyncio
import json
import time
from base64 import b64decode, b64encode
from random import choice
import aiohttp
from curl_cffi import requests
from aiohttp import WSMsgType
import uuid

from better_proxy import Proxy

from core.utils.exception import WebsocketClosedException, ProxyForbiddenException, ProxyError

import os, base64

from data.config import NODE_TYPE, USE_WSS


class GrassWs:
    def __init__(self, user_agent: str = None, proxy: str = None):
        self.user_agent = user_agent
        self.proxy = proxy
        self.destination = None
        self.token = None
        self.session = None
        self.websocket = None
        self.id = None
        self.last_live_timestamp = time.time()
        # self.ws_session = None

    async def get_addr(self, browser_id: str, user_id: str):
        
        message = {
            "browserId": browser_id,
	        "userId": user_id,
	        "version": "5.1.1",
	        "extensionId": "lkbnfiajjmbhnfledhphioinpickokdi",
	        "userAgent": self.user_agent,
	        "deviceType": "extension"
                }

        headers = {
                'User-Agent': self.user_agent,
                'Accept': '*/*',
                'Origin': 'chrome-extension://lkbnfiajjmbhnfledhphioinpickokdi',
                'Content-Type': 'application/json',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Storage-Access': 'active',
                'Accept-Encoding': 'gzip, deflate, br, zstd',
                'Accept-Language': 'en-US;q=0.8,en;q=0.7',
        }
        try:
            response = requests.post(
                'https://director.getgrass.io/checkin',
                json=message,
                headers=headers,
                proxies={'http': self.proxy, 'https': self.proxy} if self.proxy else None,
                impersonate="chrome" ,
                verify=False,
                timeout=30
            )

            if response.status_code == 201:
                try:
                    data = response.json()
                    self.destination = data.get('destinations')[0] if data.get('destinations') else None
                    self.token = data.get('token')

                    # Проверяем, что получили все необходимые данные
                    if not self.destination or not self.token:
                        #print(f"Incomplete data received: destination={self.destination}, token={self.token}")
                        raise ProxyError(f"Incomplete data from server: {data}")

                    return self.destination, self.token
                except (ValueError, json.JSONDecodeError) as e:
                    #print(f"JSON decode error: {e}, response: {response.text}")
                    raise ProxyError(f"JSON decode error: {e}")
            else:
                #print(f"Failed to get connection info: {response.status_code}, response: {response.text}")
                raise ProxyError(f"Failed to get connection info: {response.status_code}")

        except requests.exceptions.SSLError as e:
            #print(f"SSL error with proxy: {e}")
            raise ProxyError(f"SSL error with proxy: {e}")
        except requests.exceptions.ProxyError as e:
            if "connection to proxy closed" in str(e):
                #print(f"Proxy connection closed: {e}")
                raise ProxyError("Proxy connection closed")
            #print(f"Proxy error: {e}")
            raise ProxyError(f"Proxy error: {e}")
        except Exception as e:
            #print(f"Error getting connection info: {type(e).__name__}: {e}")
            if "connection to proxy closed" in str(e):
                raise ProxyError("Proxy connection closed")
            raise ProxyError(f"Error getting connection info: {e}")

    async def connect(self):
        protocol = "wss" if USE_WSS else "ws"
        uri = f"{protocol}://{self.destination}/?token={self.token}"

        random_bytes = os.urandom(16)
        sec_websocket_key = base64.b64encode(random_bytes).decode('utf-8')

        # Точный набор заголовков как в Charles
        headers = {
            'Host': self.destination,
            'Connection': 'Upgrade',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
            'User-Agent': self.user_agent,
            'Upgrade': 'websocket',
            'Origin': 'chrome-extension://lkbnfiajjmbhnfledhphioinpickokdi',
            'Sec-WebSocket-Version': '13',
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-WebSocket-Key': sec_websocket_key,
            'Sec-WebSocket-Extensions': 'permessage-deflate; client_max_window_bits',
            'Accept': ''
        }

        try:
            self.websocket = await self.session.ws_connect(
                uri,
                headers=headers,
                proxy=self.proxy,
                ssl=USE_WSS  # Используем SSL только для WSS
            )

        except Exception as e:
            if 'status' in dir(e) and e.status == 403:
                raise ProxyForbiddenException(f"Low proxy score. Can't connect. Error: {e}")
            raise e

    async def send_message(self, message):
        await self.websocket.send_str(message)

    async def receive_message(self):
        msg = await self.websocket.receive()
        if msg.type == WSMsgType.CLOSED:
            raise WebsocketClosedException(f"Websocket closed: {msg}")
        return json.loads(msg.data)

    async def get_connection_id(self):
        return await self.receive_message()

    async def action_extension(self, browser_id: str, user_id: str):
        # Получаем сообщение от сервера
        received_message = await self.get_connection_id()
        message_id = received_message.get("id")
        action = received_message.get("action")
        data = received_message.get("data", {})
        url = data.get("url", "")


        if action == "HTTP_REQUEST":
            result = await self.perform_http_request(data)

            response = {
                "id": message_id,
                "origin_action": action,
                "result": result
            }

            await self.send_message(json.dumps(response))
        elif action == "PONG":
            response = {
                "id": message_id,
                "origin_action": action
            }
            await self.send_message(json.dumps(response))

    async def perform_http_request(self, params: dict) -> dict:
        headers = params.get("headers", {})
        method = params.get("method", "GET")
        url = params["url"]
        body = params.get("body")

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                data=body,
                proxies={'http': self.proxy, 'https': self.proxy} if self.proxy else None,
                impersonate="chrome",
                verify=False,
                timeout=30
            )
            
            # Извлечение заголовков
            headers_dict = dict(response.headers)
            
            # Получаем тело ответа и кодируем в base64
            body_bytes = response.content
            body_base64 = b64encode(body_bytes).decode('utf-8')

            return {
                "url": str(response.url),
                "status": response.status_code,
                "status_text": response.reason,
                "headers": headers_dict,
                "body": body_base64
            }
        except Exception as e:
            print(f"Error occurred while performing fetch: {e}")
            return {
                "url": url,
                "status": 400,
                "status_text": "Bad Request",
                "headers": {},
                "body": ""
            }    

    async def send_ping(self):
        message = json.dumps(
            {"id": str(uuid.uuid4()), "version": "1.0.0", "action": "PING", "data": {}}
        )

        await self.send_message(message)
