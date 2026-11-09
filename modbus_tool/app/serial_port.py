"""Linux termios serial port. Stdlib only."""

import os
import select
import termios
import time

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

BAUD_MAP = {
    1200: termios.B1200,
    2400: termios.B2400,
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


class SerialError(Exception):
    pass


def port_error_text(exc: OSError, path: str) -> str:
    err = exc.errno
    if err == 2:
        return f"устройство не найдено: {path}"
    if err in (13, 1):
        return f"нет доступа к {path}"
    if err == 16:
        return f"порт занят: {path}. Освободите хаб Modbus или закройте другую программу."
    return f"порт {path}: {exc.strerror or exc}"


class SerialPort:
    def __init__(self):
        self.fd = None
        self.path = None

    @property
    def open(self) -> bool:
        return self.fd is not None

    def _release_exclusive(self, fd: int) -> None:
        if fcntl is not None and hasattr(termios, "TIOCNXCL"):
            try:
                fcntl.ioctl(fd, termios.TIOCNXCL)
            except OSError:
                pass

    def close(self) -> None:
        fd = self.fd
        if fd is None:
            return
        self.fd = None
        self._release_exclusive(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    def configure(self, path: str, baud: int, parity: str, stopbits: int, bytesize: int = 8) -> None:
        baud = int(baud)
        if baud not in BAUD_MAP:
            raise SerialError(f"неподдерживаемая скорость {baud}")
        parity = (parity or "N").upper()
        if parity not in ("N", "E", "O"):
            raise SerialError("чётность: N, E или O")
        stopbits = int(stopbits)
        if stopbits not in (1, 2):
            raise SerialError("стоп-биты: 1 или 2")
        bytesize = int(bytesize)
        sizes = {5: termios.CS5, 6: termios.CS6, 7: termios.CS7, 8: termios.CS8}
        if bytesize not in sizes:
            raise SerialError("биты данных: 5, 6, 7 или 8")
        self.close()
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise SerialError(port_error_text(exc, path)) from exc
        try:
            attrs = termios.tcgetattr(fd)
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
            iflag &= ~(
                termios.IGNBRK
                | termios.BRKINT
                | termios.PARMRK
                | termios.ISTRIP
                | termios.INLCR
                | termios.IGNCR
                | termios.ICRNL
                | termios.IXON
                | getattr(termios, "IXOFF", 0)
            )
            oflag &= ~termios.OPOST
            lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
            cflag &= ~(
                termios.CSIZE
                | termios.PARENB
                | termios.PARODD
                | termios.CSTOPB
                | getattr(termios, "CRTSCTS", 0)
            )
            cflag |= sizes[bytesize] | termios.CREAD | termios.CLOCAL
            if parity == "E":
                cflag |= termios.PARENB
            elif parity == "O":
                cflag |= termios.PARENB | termios.PARODD
            if stopbits == 2:
                cflag |= termios.CSTOPB
            speed = BAUD_MAP[baud]
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            termios.tcsetattr(
                fd,
                termios.TCSANOW,
                [iflag, oflag, cflag, lflag, speed, speed, cc],
            )
            termios.tcflush(fd, termios.TCIOFLUSH)
            if fcntl is not None and hasattr(termios, "TIOCEXCL"):
                try:
                    fcntl.ioctl(fd, termios.TIOCEXCL)
                except OSError:
                    pass
        except Exception:
            self._release_exclusive(fd)
            os.close(fd)
            raise
        self.fd = fd
        self.path = path

    def flush_input(self) -> None:
        if self.fd is not None:
            termios.tcflush(self.fd, termios.TCIFLUSH)

    def write(self, data: bytes) -> None:
        if self.fd is None:
            raise SerialError("порт закрыт")
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.fd, view)
            except BlockingIOError:
                select.select([], [self.fd], [], 1.0)
                continue
            view = view[written:]
        termios.tcdrain(self.fd)

    def read_until_silence(self, timeout: float, gap: float) -> tuple:
        """Return (data, seconds from this call until the first byte).

        The clock starts after the caller has drained TX, so adapter loopback
        shows up as a near-zero delay and a real slave does not.
        """
        if self.fd is None:
            raise SerialError("порт закрыт")
        started = time.monotonic()
        deadline = started + max(timeout, 0.01)
        buf = bytearray()
        first = None
        last = None
        while True:
            now = time.monotonic()
            if buf and last is not None and (now - last) >= gap:
                break
            if now >= deadline:
                break
            wait = deadline - now
            if buf and last is not None:
                wait = min(wait, max(0.0, gap - (now - last)))
            ready, _, _ = select.select([self.fd], [], [], max(wait, 0.0))
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 512)
            except BlockingIOError:
                continue
            if not chunk:
                continue
            now = time.monotonic()
            if first is None:
                first = now
            buf += chunk
            last = now
        delay = None if first is None else first - started
        return bytes(buf), delay
