import asyncio
import json
import logging
from collections import defaultdict
from functools import wraps
from typing import Any, Callable, List, Dict, TypeVar, DefaultDict

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidMessage,
    ConnectionClosedOK,
)
from typing_extensions import ParamSpec

from realtime.channel import Channel
from realtime.exceptions import NotConnectedError
from realtime.message import HEARTBEAT_PAYLOAD, PHOENIX_CHANNEL, ChannelEvents, Message


T_Retval = TypeVar("T_Retval")
T_ParamSpec = ParamSpec("T_ParamSpec")

logging.basicConfig(
    format="%(asctime)s:%(levelname)s - %(message)s", level=logging.INFO
)


def ensure_connection(func: Callable[T_ParamSpec, T_Retval]):
    @wraps(func)
    def wrapper(*args: T_ParamSpec.args, **kwargs: T_ParamSpec.kwargs) -> T_Retval:
        if not args[0].connected:
            raise NotConnectedError(func.__name__)

        return func(*args, **kwargs)

    return wrapper


class Socket:
    def __init__(
        self,
        url: str,
        auto_reconnect: bool = False,
        params: Dict[str, Any] = {},
        hb_interval: int = 30,
        version: int = 2,
    ) -> None:
        """
        `Socket` is the abstraction for an actual socket connection that receives and 'reroutes' `Message` according to its `topic` and `event`.
        Socket-Channel has a 1-many relationship.
        Socket-Topic has a 1-many relationship.
        :param url: Websocket URL of the Realtime server. starts with `ws://` or `wss://`
        :param params: Optional parameters for connection.
        :param hb_interval: WS connection is kept alive by sending a heartbeat message. Optional, defaults to 30.
        :param version: phoenix JSON serializer version.
        """
        self.url = url
        self.channels = defaultdict(list)
        self.connected = False
        self.params = params
        self.hb_interval = hb_interval
        self.ws_connection: websockets.client.WebSocketClientProtocol
        self.kept_alive = set()
        self.auto_reconnect = auto_reconnect
        self.version = version

        self.channels: DefaultDict[str, List[Channel]] = defaultdict(list)

    @ensure_connection
    async def listen(self) -> None:
        """
        An infinite loop that keeps listening.
        :return: None
        """
        self.kept_alive.add(asyncio.ensure_future(self.keep_alive()))

        while True:
            try:
                msg = await self.ws_connection.recv()
                if self.version == 1:
                    msg = Message(**json.loads(msg))
                elif self.version == 2:
                    msg_array = json.loads(msg)
                    msg = Message(
                        join_ref=msg_array[0],
                        ref=msg_array[1],
                        topic=msg_array[2],
                        event=msg_array[3],
                        payload=msg_array[4],
                    )
                if msg.event == ChannelEvents.reply:
                    for channel in self.channels.get(msg.topic, []):
                        if msg.ref == channel.control_msg_ref:
                            if msg.payload["status"] == "error":
                                logging.info(
                                    f"Error joining channel: {msg.topic} - {msg.payload['response']['reason']}"
                                )
                                break
                            elif msg.payload["status"] == "ok":
                                logging.info(f"Successfully joined {msg.topic}")
                                continue
                        else:
                            for cl in channel.listeners:
                                if cl.ref in ["*", msg.ref]:
                                    cl.callback(msg.payload)

                if msg.event == ChannelEvents.close:
                    for channel in self.channels.get(msg.topic, []):
                        if msg.join_ref == channel.join_ref:
                            logging.info(f"Successfully left {msg.topic}")
                            continue

                for channel in self.channels.get(msg.topic, []):
                    for cl in channel.listeners:
                        if cl.event in ["*", msg.event]:
                            cl.callback(msg.payload)

            except ConnectionClosedOK:
                logging.info("Connection was closed normally.")
                await self.leave_all()
                break

            except InvalidMessage:
                logging.error(
                    "Received an invalid message. Check message format and content."
                )

            except ConnectionClosed as e:
                logging.error(f"Connection closed unexpectedly: {e}")
                await self._handle_reconnection()

            except InvalidHandshake:
                logging.error(
                    "Invalid handshake while connecting. Ensure your client and server configurations match."
                )

            except asyncio.CancelledError:
                logging.info("Listen task was cancelled.")
                await self.leave_all()

            except (
                Exception
            ) as e:  # A general exception handler should be the last resort
                logging.error(f"Unexpected error in listen: {e}")
                await self._handle_reconnection()

    async def connect(self) -> None:
        ws_connection = await websockets.connect(self.url)

        if ws_connection.open:
            self.ws_connection = ws_connection
            self.connected = True
            logging.info("Connection was successful")
        else:
            raise Exception("Connection Failed")

    async def _handle_reconnection(self) -> None:
        if self.auto_reconnect:
            logging.info("Connection with server closed, trying to reconnect...")
            await self.connect()
            for topic, channels in self.channels.items():
                for channel in channels:
                    await channel.join()
        else:
            logging.exception("Connection with the server closed.")

    async def leave_all(self) -> None:
        for channel in self.channels:
            for chan in self.channels.get(channel, []):
                await chan.leave()

    async def keep_alive(self) -> None:
        """
        Sending heartbeat to server every 5 seconds
        Ping - pong messages to verify connection is alive
        """
        while True:
            try:
                if self.version == 1:
                    data = dict(
                        topic=PHOENIX_CHANNEL,
                        event=ChannelEvents.heartbeat,
                        payload=HEARTBEAT_PAYLOAD,
                        ref=None,
                    )
                elif self.version == 2:
                    # [null,"4","phoenix","heartbeat",{}]
                    data = [
                        None,
                        None,
                        PHOENIX_CHANNEL,
                        ChannelEvents.heartbeat,
                        HEARTBEAT_PAYLOAD,
                    ]

                await self.ws_connection.send(json.dumps(data))
                await asyncio.sleep(self.hb_interval)

            except ConnectionClosed:
                logging.error(
                    "Connection closed unexpectedly during heartbeat. Ensure the server is alive and responsive."
                )
                await self._handle_reconnection()

            except (
                Exception
            ) as e:  # A general exception handler should be the last resort
                logging.error(f"Unexpected error in keep_alive: {e}")

    @ensure_connection
    def set_channel(self, topic: str) -> Channel:
        """
        :param topic: Initializes a channel and creates a two-way association with the socket
        :return: Channel
        """
        chan = Channel(self, topic, self.params)
        self.channels[topic].append(chan)

        return chan

    def summary(self) -> None:
        """
        Prints a list of topics and event, and reference that the socket is listening to
        :return: None
        """
        for topic, chans in self.channels.items():
            for chan in chans:
                print(
                    f"Topic: {topic} | Events: {[e for e, _, _ in chan.listeners]} | References: {[r for _, r, _ in chan.listeners]}]"
                )
