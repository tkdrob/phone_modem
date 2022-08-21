"""A modem implementation designed for Home Assistant.

Supports caller ID and call rejection.
For more details about this platform, please refer to the documentation at
https://github.com/tkdrob/phone_modem
Original work credited to tgvitz@gmail.com:
https://github.com/vroomfonde1/basicmodem
"""
import asyncio
import logging
import wave
from collections.abc import Callable
from datetime import datetime

from aioserial import AioSerial, SerialException

from . import exceptions

_LOGGER = logging.getLogger(__name__)
DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_CMD_CALLERID = "AT+VCID=1"
READ_RING_TIMEOUT = 10
READ_IDLE_TIMEOUT = None


class PhoneModem:  # pylint: disable=too-many-instance-attributes
    """Implementation of modem."""

    STATE_IDLE = "idle"
    STATE_RING = "ring"
    STATE_CALLERID = "callerid"
    STATE_FAILED = "failed"

    def __init__(
        self,
        port=DEFAULT_PORT,
        incomingcallback: Callable | None = None,
        retry: bool = True,
    ) -> None:
        """Initialize internal variables."""
        self.port = port
        self.incomingcallnotificationfunc = (
            incomingcallback or self._placeholdercallback
        )
        self.retry = retry
        self.state: str = self.STATE_FAILED
        self.cmd_callerid = DEFAULT_CMD_CALLERID
        self.cmd_response = ""
        self.cmd_responselines: list[str] = []
        self.cid_time = datetime.now()
        self.cid_name: str = ""
        self.cid_number: str = ""
        self.ser = None
        self.vsm_method = 1

    async def test(self, port: str = DEFAULT_PORT) -> None:
        """Test the modem."""
        await self.initialize(port, test=True)

    async def initialize(self, port: str = DEFAULT_PORT, test: bool = False) -> None:
        """Initialize modem."""
        self.port = port

        try:
            self.ser = AioSerial(port=port)
        except SerialException as ex:
            self.ser = None
            raise exceptions.SerialError from ex

        asyncio.create_task(self._modem_sm())

        try:
            await self._sendcmd("AT")
            if self._get_response() == "":
                _LOGGER.error("No response from modem on port %s", port)
                await self.close()
                raise exceptions.ResponseError
            await self._sendcmd(self.cmd_callerid)
            if self._get_response() in ("", "ERROR"):
                _LOGGER.error("Error enabling caller id on modem")
                await self.close()
                raise exceptions.ResponseError
            if _LOGGER.level == 10:
                await self._sendcmd("ATE1")
            await self._set_class()
            for i in await self._sendcmd("AT+VSM=?"):
                if '128,"8-BIT LINEAR"' in i:
                    self.vsm_method = 128
                    break
            await self._set_class(0)

        except SerialException:
            _LOGGER.error("Unable to communicate with modem on port %s", port)
            self.ser = None

        if test:
            return await self.close()

        _LOGGER.debug("Opening port %s", port)

    def registercallback(self, incomingcallback: Callable | None = None) -> None:
        """Register/unregister callback."""
        self.incomingcallnotificationfunc = (
            incomingcallback or self._placeholdercallback
        )

    async def _read(self, timeout: float = 1.0) -> bytes:
        """Read from modem port, return null string on timeout."""
        if self.ser:
            self.ser.timeout = timeout
        else:
            return b""
        return await self.ser.readline_async()

    async def _write(self, cmd: str = "AT") -> int:
        """Write string to modem, returns number of bytes written."""
        self.cmd_response = ""
        self.cmd_responselines = []
        if self.ser is None:
            return 0
        cmd += "\r\n"
        return await self.ser.write_async(cmd.encode())

    async def _sendcmd(self, cmd: str = "AT", timeout: float = 1.0) -> list[str]:
        """Send command, wait for response. returns response from modem."""
        if await self._write(cmd):
            while self._get_response() == "" and timeout > 0:
                await asyncio.sleep(0.1)
                timeout -= 0.1
        return self._get_lines()

    def _placeholdercallback(self, newstate: str) -> None:
        """Do nothing."""
        _LOGGER.debug("placeholder callback: %s", newstate)

    async def _set_state(self, state: str) -> None:
        """Set the state."""
        self.state = state

    def _get_response(self) -> str:
        """Return completion code from modem (OK, ERROR, null string)."""
        return self.cmd_response

    def _get_lines(self) -> list[str]:
        """Return response from last modem command, including blank lines."""
        return self.cmd_responselines

    async def close(self) -> None:
        """Close modem port, exit worker thread."""
        if self.ser:
            self.ser.close()
            self.ser = None

    async def _open(self) -> None:
        """Open modem port."""
        if self.ser:
            self.ser.open()

    async def _reset(self) -> None:
        """Reset modem."""
        await self.close()
        await asyncio.sleep(0.5)
        await self.initialize(self.port)

    async def _retry(self) -> None:
        """Retry connecting.

        This goes on forever in the state machine thread until connection is regained."""
        await asyncio.sleep(10)
        try:
            return await self._reset()
        except exceptions.SerialError:
            await self._retry()

    async def _modem_sm(  # pylint: disable=[too-many-statements, too-many-branches]
        self, timeout: int | None = READ_IDLE_TIMEOUT
    ) -> None:
        """Handle modem response state machine."""
        read_timeout = timeout
        while self.ser:
            try:
                resp = await self._read(read_timeout)
            except (SerialException, SystemExit, TypeError):
                # Sleep a bit to allow main thread to remove serial
                await asyncio.sleep(0.1)
                if self.ser and self.retry:
                    _LOGGER.debug("Unable to read from port %s", self.port)
                    return await self._retry()
                break

            if self.state != self.STATE_IDLE and not resp:
                read_timeout = READ_IDLE_TIMEOUT
                await self._set_state(self.STATE_IDLE)
                self.incomingcallnotificationfunc(self.state)
                continue

            resp = resp.decode()
            resp = resp.strip("\r\n")
            if self.cmd_response == "":
                self.cmd_responselines.append(resp)
            _LOGGER.debug("mdm: %s", resp)

            if resp in ("OK", "ERROR"):
                self.cmd_response = resp
                continue

            if resp == "RING":
                if self.state == self.STATE_IDLE:
                    self.cid_name = ""
                    self.cid_number = ""
                    self.cid_time = datetime.now()

                await self._set_state(self.STATE_RING)
                self.incomingcallnotificationfunc(self.state)
                read_timeout = READ_RING_TIMEOUT
                continue

            if len(resp) <= 4 or resp.find("=") == -1:
                continue

            read_timeout = READ_RING_TIMEOUT
            cid_field, cid_data = resp.split("=")
            cid_field = cid_field.strip()
            cid_data = cid_data.strip()
            if cid_field == "DATE":
                self.cid_time = datetime.now()
                continue

            if cid_field == "NMBR":
                self.cid_number = cid_data
                continue

            if cid_field == "NAME":
                self.cid_name = cid_data
                await self._set_state(self.STATE_CALLERID)
                self.incomingcallnotificationfunc(self.state)
                _LOGGER.debug(
                    "CID: %s %s %s",
                    self.cid_time.strftime("%I:%M %p"),
                    self.cid_name,
                    self.cid_number,
                )
                try:
                    await self._write(self.cmd_callerid)
                except SerialException:
                    _LOGGER.error("Unable to write to port %s", self.port)
                    break

        await self._set_state(self.STATE_FAILED)
        _LOGGER.debug("Exiting modem state machine")

    async def accept_call(self) -> None:
        """Accept an incoming call."""
        await self._sendcmd("ATA")

    async def reject_call(self) -> None:
        """Reject an incoming call.

        Answers the call and immediately hangs up to correctly
        terminate the call.
        """
        await self.accept_call()
        await self.hangup_call()

    async def hangup_call(self) -> None:
        """Terminate the currently ongoing call."""
        await self._set_class()
        await self._sendcmd("ATH")

    async def _set_class(self, mode: int = 8) -> None:
        """Set the mode for the modem."""
        await self._sendcmd(f"AT+FCLASS={mode}")

    async def send_audio(
        self,
        file: str,
        vsm_method: int | None = None,
        sample_rate: int = 8000,
        interval: float = 0.12,
    ) -> None:
        """Send a wave audio file recorded with Audacity. Works regardless of a connected call.

        Recommended 8000Hz Mono Unsigned 8-bit PCM. Adjust interval if audio sounds choppy."""
        assert self.ser is not None
        audio = wave.open(file, "rb")
        await self._set_class()
        await self._sendcmd(f"AT+VSM={vsm_method or self.vsm_method},{sample_rate}")
        await self._sendcmd("AT+VLS=1")
        await self._sendcmd("AT+VTX")
        await asyncio.sleep(1)
        while frame := audio.readframes(1024):
            await self.ser.write_async(frame)
            await asyncio.sleep(interval)

        await self._reset()
