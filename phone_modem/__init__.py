"""A modem implementation designed for Home Assistant.

Supports caller ID and call rejection.
For more details about this platform, please refer to the documentation at
https://github.com/tkdrob/phone_modem
Original work credited to tgvitz@gmail.com:
https://github.com/vroomfonde1/basicmodem
"""
import asyncio
from datetime import datetime
import logging

import aioserial

from . import exceptions

_LOGGER = logging.getLogger(__name__)
DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_CMD_CALLERID = "AT+VCID=1"
READ_RING_TIMEOUT = 10
READ_IDLE_TIMEOUT = None


class PhoneModem:
    """Implementation of modem."""

    STATE_IDLE = "idle"
    STATE_RING = "ring"
    STATE_CALLERID = "callerid"
    STATE_FAILED = "failed"

    def __init__(self, port=DEFAULT_PORT, incomingcallback=None):
        """Initialize internal variables."""
        self.port = port
        self.incomingcallnotificationfunc = (
            incomingcallback or self._placeholdercallback
        )
        self._state = self.STATE_FAILED
        self.cmd_callerid = DEFAULT_CMD_CALLERID
        self.cmd_response = ""
        self.cmd_responselines = []
        self.cid_time = 0
        self.cid_name = ""
        self.cid_number = ""
        self.ser = None

    async def test(self, port=DEFAULT_PORT):
        """Test the modem."""
        try:
            self.ser = aioserial.AioSerial(port=port)
        except (aioserial.SerialException) as ex:
            self.ser = None
            raise exceptions.SerialError from ex

    async def initialize(self, port=DEFAULT_PORT):
        """Initialize modem."""
        self.port = port
        await self.test(port)

        _LOGGER.debug("Opening port %s", self.port)

        asyncio.create_task(self._modem_sm())

        try:
            await self._sendcmd("AT")
            if self._get_response() == "":
                _LOGGER.error("No response from modem on port %s", self.port)
                self.ser.close()
                self.ser = None
                return
            await self._sendcmd(self.cmd_callerid)
            if self._get_response() in ["", "ERROR"]:
                _LOGGER.error("Error enabling caller id on modem")
                self.ser.close()
                self.ser = None
                return
        except aioserial.SerialException:
            _LOGGER.error("Unable to communicate with modem on port %s", self.port)
            self.ser = None
        await self._set_state(self.STATE_IDLE)

    def registercallback(self, incomingcallback=None):
        """Register/unregister callback."""
        self.incomingcallnotificationfunc = (
            incomingcallback or self._placeholdercallback
        )

    async def _read(self, timeout=1.0):
        """Read from modem port, return null string on timeout."""
        self.ser.timeout = timeout
        if self.ser is None:
            return ""
        return await self.ser.readline_async()

    async def _write(self, cmd="AT"):
        """Write string to modem, returns number of bytes written."""
        self.cmd_response = ""
        self.cmd_responselines = []
        if self.ser is None:
            return 0
        cmd += "\r\n"
        return await self.ser.write_async(cmd.encode())

    async def _sendcmd(self, cmd="AT", timeout=1.0):
        """Send command, wait for response. returns response from modem."""
        if await self._write(cmd):
            while self._get_response() == "" and timeout > 0:
                await asyncio.sleep(0.1)
                timeout -= 0.1
        return self._get_lines()

    # pylint: disable = no-self-use
    def _placeholdercallback(self, newstate):
        """Do nothing."""
        _LOGGER.debug("placeholder callback: %s", newstate)

    async def _set_state(self, state):
        """Set the state."""
        self._state = state
        return

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def get_cidname(self):
        """Return last collected caller id name field."""
        return self.cid_name

    @property
    def get_cidnumber(self):
        """Return last collected caller id number."""
        return self.cid_number

    @property
    def get_cidtime(self):
        """Return time of last call."""
        return self.cid_time

    def _get_response(self):
        """Return completion code from modem (OK, ERROR, null string)."""
        return self.cmd_response

    def _get_lines(self):
        """Return response from last modem command, including blank lines."""
        return self.cmd_responselines

    async def close(self):
        """Close modem port, exit worker thread."""
        if self.ser:
            self.ser.close()
            self.ser = None
        return

    async def _modem_sm(self, timeout=READ_IDLE_TIMEOUT):
        """Handle modem response state machine."""
        read_timeout = timeout
        while self.ser:
            try:
                resp = await self._read(read_timeout)
            except (aioserial.SerialException, SystemExit, TypeError):
                _LOGGER.debug("Unable to read from port %s", self.port)
                break

            if self.state != self.STATE_IDLE and len(resp) == 0:
                read_timeout = READ_IDLE_TIMEOUT
                await self._set_state(self.STATE_IDLE)
                self.incomingcallnotificationfunc(self.state)
                continue

            resp = resp.decode()
            resp = resp.strip("\r\n")
            if self.cmd_response == "":
                self.cmd_responselines.append(resp)
            _LOGGER.debug("mdm: %s", resp)

            if resp in ["OK", "ERROR"]:
                self.cmd_response = resp
                continue

            if resp in ["RING"]:
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
            if cid_field in ["DATE"]:
                self.cid_time = datetime.now()
                continue

            if cid_field in ["NMBR"]:
                self.cid_number = cid_data
                continue

            if cid_field in ["NAME"]:
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
                except aioserial.SerialException:
                    _LOGGER.error("Unable to write to port %s", self.port)
                    break

            continue

        await self._set_state(self.STATE_FAILED)
        _LOGGER.debug("Exiting modem state machine")

    async def accept_call(self, port=DEFAULT_PORT):
        """Accept an incoming call."""
        if self.port != port:
            self.initialize(port)
        await self._sendcmd("ATA")

    async def reject_call(self, port=DEFAULT_PORT):
        """Reject an incoming call.

        Answers the call and immediately hangs up to correctly
        terminate the call.
        """
        await self.accept_call(port)
        await self.hangup_call(port)

    async def hangup_call(self, port=DEFAULT_PORT):
        """Terminate the currently ongoing call."""
        if self.port != port:
            await self.initialize(port)
        await self._sendcmd("AT+FCLASS=8")
        await self._sendcmd("ATH")
