import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from functools import cache
from typing import Annotated

from fastapi import Depends, WebSocket
from pydantic import BaseModel, ValidationError

from app.entities import EntitiesRegistry
from app.entities.call_ent import CallEntityClass, CallFault, CallRefused, OpenCall
from app.entities.chat_ent import ChatEntityClass, ChatTurn
from app.routers.link.models import LinkModels

# How long one frame may take to be taken by the client before the client counts as gone.
_SEND_SECONDS = 10.0

# What a `call.start` is told while a call is open. The app tells this `error` from any
# other by these exact words (see `docs/chat-contract.md`), so they are not to change
# without telling it.
_CALL_ALREADY_OPEN = "a call is already open"

# How many frames of mic audio may wait to be fed into a call: about half a minute. The
# socket is read whatever the call does, so audio that speech recognition does not take
# piles up, and a call that far behind is of no use: it is failed.
_MIC_FRAMES_WAITING = 300

_log = logging.getLogger(__name__)

type _Send = Callable[[BaseModel], Awaitable[None]]
type _SendAudio = Callable[[bytes], Awaitable[None]]


class LinkServiceV1Class:
    def __init__(
        self, chat: ChatEntityClass | None = None, call: CallEntityClass | None = None
    ) -> None:
        # Tests hand in their own. The app uses the ones the registry made at startup.
        self.chat = EntitiesRegistry.Chat if chat is None else chat
        self.call = EntitiesRegistry.Call if call is None else call

    async def link(self, websocket: WebSocket) -> None:
        """Serve one standing socket until the client drops it.

        Text frames are JSON with a `type`. The client sends `chat.send` to write to a
        gem: replies are written concurrently, one per gem, and each one's frames go to
        the socket that asked for it. It sends `call.start`, binary mic audio,
        `call.playback` and `call.end` to hold a call: one at a time on a socket, so its
        binary frames need no tag. A socket that closes cuts its replies still being
        written, which are stored all the same, as cut, and ends its call.

        This loop never waits on a reply or a call, so chat is read while a call opens
        and while its audio is fed in.
        """
        await websocket.accept()
        # Replies and a call write to the socket at once, one frame at a time, so the
        # frames of each stay in order and another's can only fall between two of them.
        sending = asyncio.Lock()
        turns: list[ChatTurn] = []
        # The socket's call: the latest one asked for, until the next replaces it.
        call: _Call | None = None
        # Set once the client has stopped reading and the socket was closed on it.
        gone = False

        async def put(write: Callable[[], Awaitable[None]]) -> None:
            nonlocal gone
            async with sending:
                try:
                    async with asyncio.timeout(_SEND_SECONDS):
                        await write()
                except TimeoutError:
                    # A client that is connected but has stopped reading. Left alone, it
                    # would hold up its gem's next message, from any socket, every other
                    # reply on this one, and its call. So it is treated as gone. The call
                    # is ended here and now, whichever frame it was: a socket closed by
                    # us may not wake the loop below, and a call left open holds speech
                    # connections open.
                    gone = True
                    if call is not None:
                        call.hang_up()
                    # Closing waits on the client too, and this holds every other frame
                    # of the socket back, so it gets no longer than a frame does.
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(_SEND_SECONDS):
                            await websocket.close()
                    raise

        async def send(frame: BaseModel) -> None:
            await put(lambda: websocket.send_text(frame.model_dump_json()))

        async def send_audio(pcm: bytes) -> None:
            await put(lambda: websocket.send_bytes(pcm))

        def busy() -> bool:
            return call is not None and not call.ending

        try:
            await send(LinkModels.V1.Output.Ready())
            while True:
                message = await websocket.receive()
                if gone or message["type"] == "websocket.disconnect":
                    # Frames a client sent before it stopped reading can still be handed
                    # over after the socket was closed on it. They are not acted on: a
                    # `call.start` among them would open a call nobody is there for.
                    return
                if (audio := message.get("bytes")) is not None:
                    # Mic audio for the socket's call. With no call, it is dropped.
                    if call is not None and busy():
                        call.hear(audio)
                    continue
                if (text := message.get("text")) is None:
                    await send(LinkModels.V1.Output.Error(text="a frame must be JSON text"))
                    continue
                try:
                    kind = LinkModels.V1.Input.Frame.model_validate_json(text).type
                except ValidationError as exc:
                    await send(LinkModels.V1.Output.Error(text=_reason(exc)))
                    continue
                if kind == "chat.send":
                    try:
                        asked = LinkModels.V1.Input.ChatSend.model_validate_json(text)
                    except ValidationError as exc:
                        await send(LinkModels.V1.Output.Error(text=_reason(exc)))
                        continue
                    turns = [turn for turn in turns if not turn.over.is_set()]
                    turns.append(self.chat.send(asked.gem, asked.text, send))
                elif kind == "call.start":
                    if busy():
                        # The running call carries on.
                        await send(LinkModels.V1.Output.Error(text=_CALL_ALREADY_OPEN))
                        continue
                    try:
                        start = LinkModels.V1.Input.CallStart.model_validate_json(text)
                    except ValidationError:
                        # A refusal, not an unreadable frame: a start that names no gem,
                        # or asks for a setting that is not offered.
                        start = None
                    # A call that is ending sends its last frame before this one's first.
                    call = _Call(self.call, start, send, send_audio, after=call)
                elif kind == "call.playback":
                    try:
                        report = LinkModels.V1.Input.CallPlayback.model_validate_json(text)
                    except ValidationError as exc:
                        await send(LinkModels.V1.Output.Error(text=_reason(exc)))
                        continue
                    if call is not None and busy():
                        call.report_delay(report.delay)
                elif kind == "call.end":
                    # With no call, there is nothing to end.
                    if call is not None:
                        call.hang_up()
                else:
                    await send(LinkModels.V1.Output.Error(text=f"unknown frame type: {kind}"))
        finally:
            for turn in turns:
                turn.cut()
            # A reply nobody asked for, about a late search result, is in no list of
            # this socket's. Chat is told the client has gone, and cuts it.
            self.chat.left(send)
            waits: list[Awaitable[object]] = [turn.over.wait() for turn in turns]
            if call is not None:
                call.hang_up()
                # The latest call waits for the one before it, so this waits for both.
                waits.append(call.task)
            # Each reply is stored before its turn is over, and the call has closed its
            # conversation before its task is done, so a socket that closes returns once
            # both are. If this handler is itself cancelled, it stops waiting, but the
            # turns and the call run on in tasks of their own and finish all the same,
            # unless the whole process ends first.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(asyncio.gather(*waits, return_exceptions=True))
            with contextlib.suppress(Exception):
                await websocket.close()


class _ClientGone(Exception):
    """A frame of the call could not be sent: the client has gone or stopped reading.

    That ends the call like any failure, but is not a fault of ours, so it is not logged.
    """


class _Call:
    """One call on a standing socket, from its `call.start` to its last frame.

    It runs in a task of its own, so the socket's loop never waits on it. The call opens,
    sends `call.ready`, then feeds the mic audio it is handed into the conversation, in
    order, forwards the conversation's events and speech, in order, and stores what the
    call adds to the gem's conversation as it goes. It ends with exactly one last frame:
    `call.ended`, `call.refused` or `call.failed`.

    The conversation holds speech connections open on the owner's paid account, so
    however the call ends it is closed, once, before that last frame: on `call.end`, on
    the socket closing, on a failure feeding audio in, sending it out or storing what was
    said, when mic audio piles up untaken, when the conversation ends by itself, when the
    opening is stopped part-way, and when this task is cancelled. Closing it stores the
    rest of what was said and lets the gem go.
    """

    def __init__(
        self,
        calls: CallEntityClass,
        start: LinkModels.V1.Input.CallStart | None,
        send: _Send,
        send_audio: _SendAudio,
        *,
        after: "_Call | None",
    ) -> None:
        self._calls = calls
        self._start = start  # None: the frame named no gem, or a setting not offered
        self._send = send
        self._send_audio = send_audio
        # Mic audio waits here from `call.start` until the call is open, so none is lost.
        self._audio: asyncio.Queue[bytes] = asyncio.Queue(_MIC_FRAMES_WAITING)
        # Whether the call is being ended because that audio was not being taken.
        self._behind = False
        self._delay: float | None = None
        self._hung_up = asyncio.Event()
        # The call, once it is open. Only `_run` closes it.
        self._conversation: OpenCall | None = None
        # Set once the call's end is decided: it then takes no more audio, and the
        # socket may start another call, whose frames wait for this one's last frame.
        self.ending = False
        self.task = asyncio.create_task(self._run(after))

    def hear(self, pcm: bytes) -> None:
        """Take a frame of mic audio. It is fed in once the call is open."""
        if self.ending:
            return
        try:
            self._audio.put_nowait(pcm)
        except asyncio.QueueFull:
            self._behind = True
            self.hang_up()

    def report_delay(self, seconds: float) -> None:
        """Take the app's sound device delay. One reported while opening is kept for later."""
        self._delay = seconds
        if self._conversation is not None:
            self._conversation.set_output_delay(seconds)

    def hang_up(self) -> None:
        """End the call, or stop its opening. Its last frame is `call.ended`."""
        self.ending = True
        self._hung_up.set()

    async def _run(self, after: "_Call | None") -> None:
        last: BaseModel = LinkModels.V1.Output.CallEnded()
        relays: list[asyncio.Task[None]] = []
        try:
            if after is not None:
                await asyncio.wait({after.task})
            if self._hung_up.is_set():
                return  # hung up while it waited for the call before it: `call.ended`
            if self._start is None:
                last = LinkModels.V1.Output.CallRefused(text="no gem, or an unknown setting")
                return
            if (refused := await self._open(self._start)) is not None:
                last = refused
                return
            conversation = self._conversation
            if conversation is None or self._hung_up.is_set():
                return  # hung up while it opened: `call.ended`, unless it fell behind
            await self._out(LinkModels.V1.Output.CallReady())
            relays = [
                asyncio.create_task(self._pump(conversation)),
                asyncio.create_task(self._forward(conversation)),
                asyncio.create_task(conversation.keep()),
            ]
            hung_up = asyncio.create_task(self._hung_up.wait())
            try:
                done, _ = await asyncio.wait(
                    {*relays, hung_up}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                hung_up.cancel()
            for task in relays:
                if task in done and (failure := task.exception()) is not None:
                    raise failure
            # Otherwise it was hung up, or the conversation ended by itself: `call.ended`.
        except CallFault as exc:
            # What was said could not be stored: the call does not carry on unsaved.
            _log.warning("a call on the link could not be kept", exc_info=exc.cause)
            last = LinkModels.V1.Output.CallFailed(text=exc.reason)
        except Exception as exc:
            # A relay failed, or `call.ready` could not be sent.
            if not isinstance(exc, _ClientGone):
                _log.warning("a call on the link broke", exc_info=exc)
            said = str(exc)
            last = LinkModels.V1.Output.CallFailed(
                text=f"the call broke: {said}" if said else "the call broke"
            )
        finally:
            self.ending = True
            if self._behind and isinstance(last, LinkModels.V1.Output.CallEnded):
                last = LinkModels.V1.Output.CallFailed(
                    text="the call broke: speech recognition is not taking the audio"
                )
            for task in relays:
                task.cancel()
            await asyncio.gather(*relays, return_exceptions=True)
            if self._conversation is not None:
                try:
                    await self._conversation.close()
                except CallFault as exc:
                    # What the call settled could not all be stored.
                    _log.warning("a call on the link could not be kept", exc_info=exc.cause)
                    last = LinkModels.V1.Output.CallFailed(text=exc.reason)
                except Exception:
                    # Said, since a close that fails part-way can leave a speech
                    # connection open. The call still ends for the app.
                    _log.warning("a call on the link did not close cleanly", exc_info=True)
            # Nothing more is sent for this call after this frame. A client that has
            # gone is not told.
            with contextlib.suppress(Exception):
                await self._send(last)

    async def _open(self, start: LinkModels.V1.Input.CallStart) -> BaseModel | None:
        """Open the conversation, unless the call is hung up first.

        Returns the frame that ends a call that was not opened, or None. The opening
        runs in a task of its own so that a hang-up can stop it. Stopping it is not
        instant, and it can finish in the same moment, so it is always waited for and a
        conversation it opened all the same is kept, to be closed.
        """
        if self._hung_up.is_set():
            return None
        first = None if start.first is None else start.first == "assistant"
        opening = asyncio.create_task(self._calls.open(start.gem, speaks_first=first))
        hung_up = asyncio.create_task(self._hung_up.wait())
        try:
            await asyncio.wait({opening, hung_up}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            hung_up.cancel()
            if not opening.done():
                opening.cancel()
                await asyncio.wait({opening})
            if not opening.cancelled() and opening.exception() is None:
                self._conversation = opening.result()
                if self._delay is not None:
                    self._conversation.set_output_delay(self._delay)
        if self._hung_up.is_set():
            return None
        if opening.cancelled():
            # Not by a hang-up: something it waited on was cancelled under it.
            return LinkModels.V1.Output.CallFailed(text="the call could not be opened")
        if isinstance(exc := opening.exception(), CallRefused):
            return LinkModels.V1.Output.CallRefused(text=exc.reason)
        if isinstance(exc, CallFault):
            _log.warning("a call on the link could not be opened", exc_info=exc.cause)
            return LinkModels.V1.Output.CallFailed(text=exc.reason)
        if exc is not None:
            _log.warning("a call on the link could not be opened", exc_info=exc)
            return LinkModels.V1.Output.CallFailed(text="the call could not be opened")
        return None

    async def _pump(self, conversation: OpenCall) -> None:
        """Feed the mic audio in, one frame at a time, in the order it came."""
        while True:
            await conversation.send_audio(await self._audio.get())

    async def _forward(self, conversation: OpenCall) -> None:
        """Send the conversation's speech and events, in order, until it ends."""
        async for output in conversation.outputs():
            await self._out(
                output
                if isinstance(output, bytes)
                else LinkModels.V1.Output.CallEvent.model_validate(
                    {"type": f"call.{output.type}", "text": output.text}
                )
            )

    async def _out(self, frame: BaseModel | bytes) -> None:
        """Send one frame of the call. A failure to send is the client's, not a fault."""
        try:
            if isinstance(frame, bytes):
                await self._send_audio(frame)
            else:
                await self._send(frame)
        except Exception as exc:
            raise _ClientGone(str(exc)) from exc


def _reason(exc: ValidationError) -> str:
    """Why a frame could not be read, in a line."""
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"the frame could not be read: {first['msg']}" + (f" ({where})" if where else "")


# Cached, so every socket gets the one service, and with it the one chat entity that
# keeps each gem to one reply at a time.
@cache
def get_link_service_v1() -> LinkServiceV1Class:
    return LinkServiceV1Class()


class LinkServiceRegistry:
    type V1 = Annotated[LinkServiceV1Class, Depends(get_link_service_v1)]
