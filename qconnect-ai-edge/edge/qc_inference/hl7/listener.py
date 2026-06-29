"""Asynchronous MLLP listener for HL7 v2 over TCP.

Analyzers push results to the edge node over MLLP (Minimal Lower Layer Protocol):
each HL7 message is framed by a start block ``<VT>`` (0x0b) and an end block
``<FS><CR>`` (0x1c 0x0d). This listener accepts connections, reassembles framed
messages, parses them with :class:`HL7Parser`, returns an ACK and hands the
parsed dict to a caller-supplied async callback (typically enqueuing it for
evaluation).

It is built on ``asyncio.start_server`` so it integrates with the FastAPI event
loop and adds nothing to the import-time dependency footprint. Connection
handling is defensive: timeouts, partial reads and parse failures are logged and
NAK'd rather than crashing the server.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from loguru import logger

from edge.qc_inference.hl7.parser import HL7Parser
from edge.qc_inference.hl7.validator import validate_message

# MLLP framing bytes.
VT = 0x0B  # start of block
FS = 0x1C  # end of block
CR = 0x0D  # carriage return (follows FS)

# Per-connection read timeout (seconds) to reclaim idle/half-open sockets.
READ_TIMEOUT_SECONDS = 30.0
# Safety cap on a single message size to avoid unbounded memory growth.
MAX_MESSAGE_BYTES = 1_048_576  # 1 MiB

# Callback receives the parsed HL7 dict; may be sync or async.
MessageCallback = Callable[[dict], Awaitable[None] | None]


class MLLPListener:
    """Async MLLP TCP server that parses HL7 messages and ACKs them."""

    def __init__(
        self,
        callback: MessageCallback,
        host: str = "0.0.0.0",
        port: int = 2575,
        *,
        parser: HL7Parser | None = None,
    ) -> None:
        """Create a listener.

        Args:
            callback: invoked with each successfully parsed HL7 message dict.
            host: bind address (default all interfaces).
            port: TCP port (HL7/MLLP default is 2575).
            parser: optional custom parser (defaults to :class:`HL7Parser`).
        """
        self._callback = callback
        self.host = host
        self.port = port
        self._parser = parser or HL7Parser()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Bind the socket and begin serving (returns once the server is up)."""
        self._server = await asyncio.start_server(
            self.handle_connection, self.host, self.port
        )
        sockets = ", ".join(str(s.getsockname()) for s in (self._server.sockets or []))
        logger.info("MLLP listener started on {}", sockets or f"{self.host}:{self.port}")

    async def serve_forever(self) -> None:
        """Start (if needed) and serve until cancelled."""
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop accepting connections and wait for the server to close."""
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001 - shutdown best-effort
                pass
            logger.info("MLLP listener stopped")
            self._server = None

    async def handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single analyzer connection, possibly multiple messages.

        Reads MLLP-framed messages until the peer closes the connection or a
        timeout/error occurs. Each message is validated, parsed, ACK'd and passed
        to the callback. Errors are isolated per-message so one bad message does
        not drop the connection.
        """
        peer = writer.get_extra_info("peername")
        logger.debug("MLLP connection from {}", peer)
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        self._read_framed_message(reader), timeout=READ_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    logger.debug("MLLP read timeout from {}", peer)
                    break
                if raw is None:
                    break  # connection closed cleanly

                await self._process_message(raw, writer, peer)
        except (ConnectionResetError, BrokenPipeError) as exc:
            logger.warning("MLLP connection error from {}: {}", peer, exc)
        except Exception as exc:  # noqa: BLE001 - never let one socket crash the loop
            logger.exception("MLLP handler error from {}: {}", peer, exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            logger.debug("MLLP connection closed: {}", peer)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _read_framed_message(
        self, reader: asyncio.StreamReader
    ) -> str | None:
        """Read one MLLP-framed message; return its body or ``None`` on EOF.

        Waits for the VT start byte, then reads until the FS+CR end block.
        """
        # Seek the start-of-block byte, discarding any inter-message noise.
        start = await reader.read(1)
        if not start:
            return None  # EOF
        while start and start[0] != VT:
            start = await reader.read(1)
            if not start:
                return None

        buffer = bytearray()
        while True:
            chunk = await reader.read(1)
            if not chunk:
                # Peer closed mid-message; treat as incomplete.
                return None
            byte = chunk[0]
            if byte == FS:
                # Expect a trailing CR; consume it if present.
                trailing = await reader.read(1)
                # If trailing wasn't CR, we still accept the message body.
                break
            buffer.append(byte)
            if len(buffer) > MAX_MESSAGE_BYTES:
                logger.warning("MLLP message exceeded {} bytes; dropping", MAX_MESSAGE_BYTES)
                return None
        return buffer.decode("utf-8", errors="replace")

    async def _process_message(
        self, raw: str, writer: asyncio.StreamWriter, peer: object
    ) -> None:
        """Validate, parse, ACK and dispatch a single HL7 message."""
        ok, reasons = validate_message(raw)
        if not ok:
            logger.warning("Rejecting invalid HL7 from {}: {}", peer, reasons)
            self._send_ack(writer, None, code="AR")
            return

        try:
            parsed = self._parser.parse_message(raw)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to parse HL7 from {}: {}", peer, exc)
            self._send_ack(writer, None, code="AE")
            return

        ctrl_id = parsed.get("message_control_id")
        # ACK first so the analyzer is freed up quickly, then dispatch.
        self._send_ack(writer, ctrl_id, code="AA")

        try:
            result = self._callback(parsed)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 - callback isolation
            logger.exception("HL7 callback error (ctrl_id={}): {}", ctrl_id, exc)

    def _send_ack(
        self, writer: asyncio.StreamWriter, ctrl_id: str | None, code: str
    ) -> None:
        """Frame and write an ACK back to the analyzer."""
        ack = self._parser.build_ack(ctrl_id, code=code)
        framed = bytes([VT]) + ack.encode("utf-8") + bytes([FS, CR])
        try:
            writer.write(framed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send ACK ({}): {}", code, exc)
