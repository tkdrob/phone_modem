"""
A modem implementation designed for Home Assistant that supports caller ID and
call rejection.

For more details about this platform, please refer to the documentation at
https://github.com/tkdrob/phone_modem

Original work credited to tgvitz@gmail.com and havocsec-os@pm.me:
https://github.com/vroomfonde1/basicmodem
https://github.com/havocsec/cx93001
"""
import logging
import os
import serial
import time
import wave
from . import exceptions
from datetime import datetime
from pydub import AudioSegment


_LOGGER = logging.getLogger(__name__)
DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_CMD_CALLERID = "AT+VCID=1"
READ_RING_TIMOUT = 10
READ_IDLE_TIMEOUT = None


class PhoneModem(object):
    """Implementation of modem."""

    STATE_IDLE = "idle"
    STATE_RING = "ring"
    STATE_CALLERID = "callerid"
    STATE_FAILED = "failed"

    def __init__(self, port=DEFAULT_PORT, incomingcallback=None):
        """Initialize internal variables."""
        import threading

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

        _LOGGER.debug("Opening port %s", self.port)
        try:
            self.ser = serial.Serial(port=self.port)
        except (serial.SerialException) as ex:
            self.ser = None
            raise exceptions.SerialError from ex

        threading.Thread(target=self._modem_sm, daemon=True).start()
        try:
            self.sendcmd("AT")
            if self.get_response() == "":
                _LOGGER.error("No response from modem on port %s", self.port)
                self.ser.close()
                self.ser = None
                return
            self.sendcmd(self.cmd_callerid)
            if self.get_response() in ["", "ERROR"]:
                _LOGGER.error("Error enabling caller id on modem.")
                self.ser.close()
                self.ser = None
                return
        except serial.SerialException:
            _LOGGER.error("Unable to communicate with modem on port %s", self.port)
            self.ser = None
        self.set_state(self.STATE_IDLE)

    def registercallback(self, incomingcallback=None):
        """Register/unregister callback."""
        self.incomingcallnotificationfunc = (
            incomingcallback or self._placeholdercallback
        )

    def read(self, timeout=1.0):
        """read from modem port, return null string on timeout."""
        self.ser.timeout = timeout
        if self.ser is None:
            return ""
        return self.ser.readline()

    def write(self, cmd="AT"):
        """write string to modem, returns number of bytes written."""
        self.cmd_response = ""
        self.cmd_responselines = []
        if self.ser is None:
            return 0
        cmd += "\r\n"
        return self.ser.write(cmd.encode())

    def sendcmd(self, cmd="AT", timeout=1.0):
        """send command, wait for response. returns response from modem."""
        import time

        if self.write(cmd):
            while self.get_response() == "" and timeout > 0:
                time.sleep(0.1)
                timeout -= 0.1
        return self.get_lines()

    # pylint: disable = no-self-use
    def _placeholdercallback(self, newstate):
        """ Does nothing."""
        _LOGGER.debug("placeholder callback: %s", newstate)
        return

    def set_state(self, state):
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
        """Returns time of last call."""
        return self.cid_time

    def get_response(self):
        """Return completion code from modem (OK, ERROR, null string)."""
        return self.cmd_response

    def get_lines(self):
        """Returns response from last modem command, including blank lines."""
        return self.cmd_responselines

    def close(self):
        """close modem port, exit worker thread."""
        if self.ser:
            self.ser.close()
            self.ser = None
        return

    def _modem_sm(self):
        """Handle modem response state machine."""
        import datetime

        read_timeout = READ_IDLE_TIMEOUT
        while self.ser:
            try:
                resp = self.read(read_timeout)
            except (serial.SerialException, SystemExit, TypeError):
                _LOGGER.debug("Unable to read from port %s", self.port)
                break

            if self.state != self.STATE_IDLE and len(resp) == 0:
                read_timeout = READ_IDLE_TIMEOUT
                self.set_state(self.STATE_IDLE)
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
                    self.cid_time = datetime.datetime.now()

                self.set_state(self.STATE_RING)
                self.incomingcallnotificationfunc(self.state)
                read_timeout = READ_RING_TIMOUT
                continue

            if len(resp) <= 4 or resp.find("=") == -1:
                continue

            read_timeout = READ_RING_TIMOUT
            cid_field, cid_data = resp.split("=")
            cid_field = cid_field.strip()
            cid_data = cid_data.strip()
            if cid_field in ["DATE"]:
                self.cid_time = datetime.datetime.now()
                continue

            if cid_field in ["NMBR"]:
                self.cid_number = cid_data
                continue

            if cid_field in ["NAME"]:
                self.cid_name = cid_data
                self.set_state(self.STATE_CALLERID)
                self.incomingcallnotificationfunc(self.state)
                _LOGGER.debug(
                    "CID: %s %s %s",
                    self.cid_time.strftime("%I:%M %p"),
                    self.cid_name,
                    self.cid_number,
                )
                try:
                    self.write(self.cmd_callerid)
                except serial.SerialException:
                    _LOGGER.error("Unable to write to port %s", self.port)
                    break

            continue

        self.set_state(self.STATE_FAILED)
        _LOGGER.debug("Exiting modem state machine")
        return

    def accept_call(self, port=DEFAULT_PORT):
        """Accepts an incoming call"""
        self.ser = serial.Serial(port)
        self.sendcmd("ATA")

    def reject_call(self, port=DEFAULT_PORT):
        """Rejects an incoming call. Answers the call and immediately hangs up in order to correctly terminate the incoming call."""
        self.ser = serial.Serial(port)
        self.accept_call(port)
        self.hangup_call(port)

    def hangup_call(self, port=DEFAULT_PORT):
        """Terminates the currently ongoing call"""
        self.sendcmd("AT+FCLASS=8")
        self.sendcmd("ATH")

    def tts_say(self, phrase, lang="english"):
        """Transmits a TTS phrase over an ongoing call

        Uses espeak and ffmpeg to generate a wav file of the phrase. Then, it's transmitted over the ongoing call.
        """
        os.system(
            "espeak -w temp.wav -v"
            + lang
            + ' "'
            + phrase
            + '" ; ffmpeg -i temp.wav -ar 8000 -acodec pcm_u8 '
            " -ac 1 phrase.wav"
        )
        os.remove("temp.wav")
        self.play_audio_file("phrase.wav")
        os.remove("../phrase.wav")

    def play_tones(self, sequence):
        """Plays a sequence of DTMF tones

        Plays a sequence of DTMF tones over an ongoing call.
        """

        self.__at("AT+VTS=" + ",".join(sequence))
        time.sleep(len(sequence))

    def play_audio_obj(self, wavobj, timeout=0):
        """Transmits a wave audio object over an ongoing call

        Transmits a wave audio object over an ongoing call. Enables voice transmit mode and the audio is
        played until it's finished if the timeout is 0 or until the timeout is reached.
        """

        if timeout == 0:
            timeout = wavobj.getnframes() / wavobj.getframerate()
        self.__at("AT+VTX")
        # print(timeout)
        chunksize = 1024
        start_time = time.time()
        data = wavobj.readframes(chunksize)
        while data != "":
            self.__con.write(data)
            data = wavobj.readframes(chunksize)
            time.sleep(0.06)
            if time.time() - start_time >= timeout:
                break

    def play_audio_file(self, wavfile, timeout=0):
        """Transmits a wave 8-bit PCM mono @ 8000Hz audio file over an ongoing call

        Transmits a wave 8-bit PCM mono @ 8000Hz audio file over an ongoing call. Enables voice transmit mode
        and the audio is played until it finished if the timeout is 0 or until the timeout is reached.
        """

        wavobj = wave.open(wavfile, "rb")
        self.play_audio_obj(wavobj, timeout=timeout)
        wavobj.close()

    def dial(self, number):
        """Initiate a call with the desired number

        Sets the modem to voice mode, sets the sampling mode to 8-bit PCM mono @ 8000 Hz, enables transmitting
        operating mode, silence detection over a period of 5 seconds and dials to the desired number.
        """

        self.__at("AT+FCLASS=8")
        self.__at("AT+VSM=1,8000,0,0")
        self.__at("AT+VLS=1")
        self.__at("AT+VSD=128,50")
        self.__at("ATD" + number)

    def record_call(self, date=datetime.now(), number="unknown", timeout=7200):
        """Records an ongoing call until it's finished or the timeout is reached

        Sets the modem to voice mode, sets the sampling mode to 8-bit PCM mono @ 8000 Hz, enables transmitting
        operating mode, silence detection over a period of 5 seconds and voice reception mode. Then, a mp3 file
        is written until the end of the call or until the timeout is reached.
        """

        self.__at("AT+FCLASS=8")
        self.__at("AT+VSM=1,8000,0,0")
        self.__at("AT+VLS=1")
        self.__at("AT+VSD=128,50")
        self.__at("AT+VRX", "CONNECT")

        chunksize = 1024
        frames = []
        start = time.time()
        while True:
            chunk = self.__con.read(chunksize)
            if self.__detect_end(chunk):
                break
            if time.time() - start >= timeout:
                # print('Timeout reached')
                break
            frames.append(chunk)
        self.hang_up()
        # Merge frames and save temporarily as .wav
        wav_path = date.strftime("%d-%m-%Y_%H:%M:%S_") + number + ".wav"
        wav_file = wave.open(wav_path, "wb")
        wav_file.setnchannels(1)
        wav_file.setsampwidth(1)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"".join(frames))
        wav_file.close()
        # Convert from .wav to .mp3 in order to save space
        segment = AudioSegment.from_wav(wav_path)
        segment.export(wav_path[:-3] + "mp3", format="mp3")
        os.remove(wav_path)