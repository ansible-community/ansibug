# Copyright (c) 2022 Jordan Borean
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import functools
import json
import logging
import os
import pathlib
import queue
import ssl
import threading
import typing as t

from . import dap
from ._mp_queue import ClientMPQueue, MPProtocol, ServerMPQueue
from ._singleton import Singleton
from ._socket_helper import CancelledError, SocketCancellationToken

HAS_DEBUGPY = True
try:  # pragma: nocover
    import debugpy

except Exception:  # pragma: nocover
    HAS_DEBUGPY = False

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class PathMapping:
    """Maps a local path to the equivalent remote path."""

    local_root: str
    remote_root: str


@dataclasses.dataclass(frozen=True)
class DebugConfiguration:
    """Debug configuration data.

    Extra debug configuration data sent from the DA server after the debuggee
    has connected. This data is used to send extra data not defined in the
    debug adapter protocol needed for debug operations.

    Args:
        path_mappings: The path mappings to use when translating source paths
            with what Ansible is running with.
    """

    OUTPUT_CATEGORY: t.ClassVar[str] = "ansibug_debug_configuration"

    path_mappings: t.List[PathMapping] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class PlaybookProcessInfo:
    pid: int
    host: str
    port: int
    is_ipv6: bool
    use_tls: bool
    playbook_file: t.Optional[str]

    @classmethod
    def from_json(
        self,
        data: dict[str, t.Any],
    ) -> PlaybookProcessInfo:
        return PlaybookProcessInfo(
            pid=data["pid"],
            host=data["host"],
            port=int(data["port"]),
            is_ipv6=data["is_ipv6"],
            use_tls=data["use_tls"],
            playbook_file=data["playbook_file"],
        )

    def to_json(self) -> dict[str, t.Any]:
        return {
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "is_ipv6": self.is_ipv6,
            "use_tls": self.use_tls,
            "playbook_file": self.playbook_file,
        }


def get_pid_info_path(pid: int) -> pathlib.Path:
    """Get the path used to store info about the ansible-playbook debug proc."""
    # It is important that changes here are also reflected in the extension code
    # so that it knows where dir and what file pattern to look for when
    # scanning for available playbooks.
    tmpdir = os.environ.get("TMPDIR", "/tmp")
    return pathlib.Path(tmpdir) / f"ANSIBUG-{pid}"


class DAProtocol(MPProtocol):
    def __init__(
        self,
        debugger: AnsibleDebugger,
    ) -> None:
        self._debugger = debugger

    def on_msg_received(
        self,
        msg: dap.ProtocolMessage,
    ) -> None:
        log.info("Processing msg %r", msg)
        try:
            self._debugger.process_message(msg)
        except Exception as e:
            log.exception("Exception while processing msg seq %d", msg.seq)

            if isinstance(msg, dap.Request):
                resp = dap.ErrorResponse(
                    command=msg.command,
                    request_seq=msg.seq,
                    message=f"Unknown error: {e!r}",
                )
                self._debugger.send(resp)

    def connection_closed(
        self,
        exp: Exception | None,
    ) -> None:
        # FIXME: log exception
        self._debugger.send(None)

    def connection_made(self) -> None:
        with self._debugger._send_queue_lock:
            self._debugger._send_queue_active = True


class DebugState(t.Protocol):
    def ended(self) -> None:
        ...  # pragma: nocover

    def evaluate(
        self,
        request: dap.EvaluateRequest,
    ) -> dap.EvaluateResponse:
        raise NotImplementedError()  # pragma: nocover

    def continue_request(
        self,
        request: dap.ContinueRequest,
    ) -> dap.ContinueResponse:
        raise NotImplementedError()  # pragma: nocover

    def get_scopes(
        self,
        request: dap.ScopesRequest,
    ) -> dap.ScopesResponse:
        raise NotImplementedError()  # pragma: nocover

    def get_stacktrace(
        self,
        request: dap.StackTraceRequest,
    ) -> dap.StackTraceResponse:
        raise NotImplementedError()  # pragma: nocover

    def get_threads(
        self,
        request: dap.ThreadsRequest,
    ) -> dap.ThreadsResponse:
        raise NotImplementedError()  # pragma: nocover

    def get_variables(
        self,
        request: dap.VariablesRequest,
    ) -> dap.VariablesResponse:
        raise NotImplementedError()  # pragma: nocover

    def set_variable(
        self,
        request: dap.SetVariableRequest,
    ) -> dap.SetVariableResponse:
        raise NotImplementedError()  # pragma: nocover

    def step_in(
        self,
        request: dap.StepInRequest,
    ) -> None:
        raise NotImplementedError()  # pragma: nocover

    def step_out(
        self,
        request: dap.StepOutRequest,
    ) -> None:
        raise NotImplementedError()  # pragma: nocover

    def step_over(
        self,
        request: dap.NextRequest,
    ) -> None:
        raise NotImplementedError()  # pragma: nocover


@dataclasses.dataclass
class AnsibleLineBreakpoint:
    id: int
    source: dap.Source
    source_breakpoint: dap.SourceBreakpoint
    breakpoint: dap.Breakpoint
    actual_path: str
    source_path: str


class AnsibleDebugger(metaclass=Singleton):
    def __init__(self) -> None:
        self._addr = ""
        self._addr_event = threading.Event()
        self._cancel_token = SocketCancellationToken()
        self._debug_config: DebugConfiguration = DebugConfiguration()
        self._recv_thread: threading.Thread | None = None
        self._send_queue: queue.Queue[dap.ProtocolMessage | None] = queue.Queue()
        self._send_queue_active = False
        self._send_queue_lock = threading.Lock()
        self._da_connected = threading.Event()
        self._configuration_done = threading.Event()
        self._proto = DAProtocol(self)
        self._strategy_connected = threading.Condition()
        self._strategy: DebugState | None = None

        self._thread_counter = 2  # 1 is always the main thread
        self._stackframe_counter = 1
        self._variable_counter = 1

        # Stores all the client breakpoints, key is the breakpoint number/id
        self._breakpoints: dict[int, AnsibleLineBreakpoint] = {}
        self._breakpoint_counter = 1

        # Key is the path, the value is a list of the lines in that file where:
        #   None - Line is a continuation of a breakpoint range
        #   0    - Line is not something a breakpoint can be set at.
        #   1    - Line is the start of a breakpoint range
        #
        # The lines are 1 based with the 0 index representing 0 meaning a
        # breakpoint cannot be set until the first valid entry is found. A
        # continuation means the behaviour of the previous int in the list
        # continues to apply at that line.
        #
        # Examples of None would be
        #   - block/rescue/always - cannot stop on this, bp needs to be on a
        #     task.
        #   - import_* - These tasks are seen as the imported value not as
        #     itself
        #
        # Known Problems:
        #   - import_* tasks aren't present in the Playbook block. According to
        #     these rules it will be set as a breakpoint for the previous entry
        #   - roles in a play act like import_role - same as above
        #   - always and rescue aren't seen as an exact entry, will be set to
        #     the task that preceeds it
        #   - Won't contain the remaining lines of the file - bp checks will
        #     just have to use the last entry
        # FIXME: Somehow detect import entries to invalidate them.
        self._source_info: dict[str, list[int | None]] = {}

    @contextlib.contextmanager
    def with_strategy(
        self,
        strategy: DebugState,
    ) -> collections.abc.Generator[None, None, None]:
        with self._strategy_connected:
            self._strategy = strategy
            self._strategy_connected.notify_all()

        try:
            if self._da_connected.is_set():
                self._configuration_done.wait()

            yield

        finally:
            with self._strategy_connected:
                self._strategy = None
                self._strategy_connected.notify_all()

    def next_thread_id(self) -> int:
        tid = self._thread_counter
        self._thread_counter += 1

        return tid

    def next_stackframe_id(self) -> int:
        sfid = self._stackframe_counter
        self._stackframe_counter += 1

        return sfid

    def next_variable_id(self) -> int:
        vid = self._variable_counter
        self._variable_counter += 1

        return vid

    def wait_for_config_done(
        self,
        timeout: float | None = 10.0,
    ) -> None:
        """Waits until the debug config is done.

        Waits until the client has sent through the configuration done request
        that indicates no more initial configuration data is expected.

        Args:
            timeout: The maximum time, in seconds, to wait until the debug
                adapter is connected and ready.
        """
        self._da_connected.wait(timeout=timeout)

    def get_breakpoint(
        self,
        path: str,
        line: int,
    ) -> AnsibleLineBreakpoint | None:
        with self._send_queue_lock:
            if not self._send_queue_active:
                return None

        for b in self._breakpoints.values():
            if (
                b.actual_path == path
                and (b.breakpoint.line is None or b.breakpoint.line <= line)
                and (b.breakpoint.end_line is None or b.breakpoint.end_line >= line)
            ):
                return b

        return None

    def start(
        self,
        host: str,
        port: int,
        mode: t.Literal["connect", "listen"],
        *,
        ssl_context: ssl.SSLContext | None = None,
        playbook_file: str | None = None,
    ) -> str:
        """Start the background server thread.

        Starts the background server thread which waits for an incoming request
        on the process' Unix Domain Socket and then subsequently starts the
        DAP server socket on the request that came in.

        Args:
            host: The socket hostname portion to connect/bind.
            port: The socket port portion to connect/bind.
            mode: The socket mode to use, connect will connect to the addr
                while listen will bind to the addr and wait for a connection.
            ssl_context: Optional client SSLContext to wrap the socket
                connection with.
            playbook_file: The file of the playbook being run, this is optional
                info used for storing metadata with the process pid on listen.

        returns:
            str: The socket address that is being used, this is only valid when
            using listen mode.
        """
        self._recv_thread = threading.Thread(
            target=self._recv_task,
            args=(host, port, mode, ssl_context, playbook_file),
            name="ansibug-debugger",
        )
        self._recv_thread.start()

        self._addr_event.wait()
        return self._addr

    def shutdown(self) -> None:
        """Shutdown the Debug Server.

        Marks the server as completed and signals the DAP server thread to
        shutdown.
        """
        log.debug("Shutting down DebugServer")
        self._send_queue.join()
        self._cancel_token.cancel()
        if self._recv_thread:
            self._recv_thread.join()

        log.debug("DebugServer is shutdown")

    def send(
        self,
        msg: dap.ProtocolMessage | None,
    ) -> None:
        with self._send_queue_lock:
            if self._send_queue_active:
                log.info("Sending to DA adapter - %r", msg)
                self._send_queue.put(msg)
            else:
                log.info("Discarding msg to DA adapter as queue is off - %r", msg)

    def convert_to_client_path(
        self,
        path: str,
    ) -> str:
        """Converts the path to the client path.

        Converts the path provided to the client path equivalent based on the
        mappings provided. This is used in cases where the paths in the client
        does not match up with what Ansible is running with, for example
        debugging on a different host with a different path root.

        Args:
            path: The path to convert.

        Returns:
            str: The converted path.
        """
        for mapping in self._debug_config.path_mappings:
            if path.startswith(mapping.remote_root):
                return mapping.local_root + path[len(mapping.remote_root) :]

        else:
            return path

    def register_path_breakpoint(
        self,
        path: str,
        line: int,
        bp_type: int,
    ) -> None:
        """Register a valid breakpoint section.

        Registers a line as a valid breakpoint in a path. This registration is
        used when responding the breakpoint requests from the client.

        Args:
            path: The file path the line is set.
            line: The line the breakpoint is registered to.
            bp_type: Set to 1 for a valid breakpoint and 0 for an invalid
                breakpoint section.
        """
        # Ensure each new entry has a starting value of 0 which denotes that
        # a breakpoint cannot be set at the start of the file. It can only be
        # set when a line was registered.
        file_lines = self._source_info.setdefault(path, [0])
        file_lines.extend([None] * (1 + line - len(file_lines)))
        file_lines[line] = bp_type

        # FIXME: Put into common location to share with SetBreakpointRequest.
        for breakpoint in self._breakpoints.values():
            if breakpoint.actual_path != path:
                continue

            source_breakpoint = breakpoint.source_breakpoint
            start_line = min(source_breakpoint.line, len(file_lines) - 1)
            end_line = start_line + 1

            line_type = file_lines[start_line]
            while line_type is None:
                start_line -= 1
                line_type = file_lines[start_line]

            while end_line < len(file_lines) and file_lines[end_line] is None:
                end_line += 1

            end_line = min(end_line - 1, len(file_lines))

            if line_type == 0:
                verified = False
                bp_msg = "Breakpoint cannot be set here."
            else:
                verified = True
                bp_msg = None

            if (
                breakpoint.breakpoint.verified != verified
                or breakpoint.breakpoint.line != start_line
                or breakpoint.breakpoint.end_line != end_line
            ):
                bp = breakpoint.breakpoint = dap.Breakpoint(
                    id=breakpoint.id,
                    verified=verified,
                    message=bp_msg,
                    source=breakpoint.source,
                    line=start_line,
                    end_line=end_line,
                )
                self.send(
                    dap.BreakpointEvent(
                        reason="changed",
                        breakpoint=bp,
                    )
                )

    @classmethod
    def _enable_debugpy(cls) -> None:  # pragma: nocover
        """This is only meant for debugging ansibug in Ansible purposes."""
        if not HAS_DEBUGPY:
            return

        elif not debugpy.is_client_connected():
            debugpy.listen(("localhost", 12535))
            debugpy.wait_for_client()

    def _get_strategy(self) -> DebugState:
        with self._strategy_connected:
            self._strategy_connected.wait_for(lambda: self._strategy is not None)
            return t.cast(DebugState, self._strategy)

    def _recv_task(
        self,
        host: str,
        port: int,
        mode: t.Literal["connect", "listen"],
        ssl_context: ssl.SSLContext | None,
        playbook_file: str | None = None,
    ) -> None:
        """Background server recv task.

        This is the task that continuously runs in the background waiting for
        DAP server to exchange debug messages. Depending on the mode requested
        the socket could be waiting for a connection or trying to connect to
        addr requested.

        In listen mode, the socket will attempt to wait for more DAP servers to
        connect to it in order to allow multiple connections as needed. This
        continues until the playbook is completed.

        Args:
            host: The socket hostname portion to connect/bind.
            port: The socket port portion to connect/bind.
            mode: The socket mode to use, connect will connect to the addr
                while listen will bind to the addr and wait for a connection.
            ssl_context: Optional client SSLContext to wrap the socket
                connection with.
            playbook_file: The file of the playbook being run, this is optional
                info used for storing metadata with the process pid on listen.
        """
        log.info("Setting up ansible-playbook debug %s socket at '%s:%d'", mode, host, port)

        queue_kwargs: dict[str, t.Any] = {
            "address": (host, port),
            "proto": lambda: self._proto,
            "ssl_context": ssl_context,
            "cancel_token": self._cancel_token,
        }
        queue_type = ClientMPQueue if mode == "connect" else ServerMPQueue

        proc_pid_file = get_pid_info_path(os.getpid())
        try:
            while True:
                with queue_type(**queue_kwargs) as mp_queue:
                    if isinstance(mp_queue, ServerMPQueue):
                        bound_host, bound_port, is_ipv6 = mp_queue.address
                        proc_info = PlaybookProcessInfo(
                            pid=os.getpid(),
                            host=bound_host,
                            port=bound_port,
                            is_ipv6=is_ipv6,
                            use_tls=ssl_context is not None,
                            playbook_file=playbook_file,
                        )
                        proc_json = json.dumps(proc_info.to_json())
                        proc_pid_file.write_text(proc_json)

                    sock_host, sock_port, is_ipv6 = mp_queue.address
                    if is_ipv6:
                        self._addr = f"[{sock_host}]:{sock_port}"
                    else:
                        self._addr = f"{sock_host}:{sock_port}"
                    self._addr_event.set()

                    mp_queue.start()
                    try:
                        self._da_connected.set()

                        while True:
                            resp = self._send_queue.get()
                            try:
                                if not resp:
                                    break
                                mp_queue.send(resp)
                            finally:
                                self._send_queue.task_done()

                    finally:
                        self._da_connected.clear()

                if mode == "connect":
                    break

        except CancelledError:
            pass

        except Exception as e:
            log.exception(f"Unknown error in DAP thread: %s", e)

        finally:
            if proc_pid_file.exists():
                proc_pid_file.unlink(missing_ok=True)

            # Ensure the queue is seen as complete so shutdown ends
            with self._send_queue_lock:
                while True:
                    try:
                        self._send_queue.get(block=False)
                    except queue.Empty:
                        break
                    else:
                        self._send_queue.task_done()

                self._send_queue_active = False

        # Ensures client isn't stuck waiting for something to never come.
        self._da_connected.set()
        self._configuration_done.set()
        with self._strategy_connected:
            if self._strategy:
                self._strategy.ended()

        log.debug("DAP server thread task ended")

    @functools.singledispatchmethod
    def process_message(
        self,
        msg: dap.ProtocolMessage,
    ) -> None:
        raise NotImplementedError(type(msg).__name__)  # pramga: nocover

    @process_message.register
    def _(
        self,
        msg: dap.ConfigurationDoneRequest,
    ) -> None:
        resp = dap.ConfigurationDoneResponse(request_seq=msg.seq)
        self.send(resp)
        self._configuration_done.set()

    @process_message.register
    def _(
        self,
        msg: dap.ContinueRequest,
    ) -> None:
        strategy = self._get_strategy()
        resp = strategy.continue_request(msg)
        self.send(resp)

    @process_message.register
    def _(
        self,
        msg: dap.EvaluateRequest,
    ) -> None:
        strategy = self._get_strategy()
        resp = strategy.evaluate(msg)
        self.send(resp)

    @process_message.register
    def _(
        self,
        msg: dap.NextRequest,
    ) -> None:
        strategy = self._get_strategy()
        strategy.step_over(msg)
        self.send(dap.NextResponse(request_seq=msg.seq))

    @process_message.register
    def _(
        self,
        msg: dap.ScopesRequest,
    ) -> None:
        strategy = self._get_strategy()
        resp = strategy.get_scopes(msg)
        self.send(resp)

    @process_message.register
    def _(
        self,
        msg: dap.SetBreakpointsRequest,
    ) -> None:
        # FIXME: Deal with source_reference if set
        source_path = msg.source.path or ""

        # If explicit path mappings are provided we convert the client provided
        # source path to the path known to Ansible. This is important as the
        # source mappings are keyed by the Ansible paths.
        actual_path = source_path
        for mapping in self._debug_config.path_mappings:
            if actual_path.startswith(mapping.local_root):
                actual_path = mapping.remote_root + actual_path[len(mapping.local_root) :]
                break

        source_info = self._source_info.get(actual_path, None)

        # Clear out existing breakpoints for the source as each request should
        # send the latest list for a source.
        self._breakpoints = {bpid: b for bpid, b in self._breakpoints.items() if b.source_path != source_path}

        breakpoint_info: list[dap.Breakpoint] = []
        for source_breakpoint in msg.breakpoints:
            bp_id = self._breakpoint_counter
            self._breakpoint_counter += 1

            bp: dap.Breakpoint
            if msg.source_modified:
                bp = dap.Breakpoint(
                    id=bp_id,
                    verified=False,
                    message="Cannot set breakpoint on a modified source.",
                    source=msg.source,
                )
                # I don't think we need to preserve this bp for later reference.
                breakpoint_info.append(bp)
                continue

            if not source_info:
                bp = dap.Breakpoint(
                    id=bp_id,
                    verified=False,
                    message="File has not been loaded by Ansible, cannot detect breakpoints yet.",
                    source=msg.source,
                    line=source_breakpoint.line,
                )

            else:
                start_line = min(source_breakpoint.line, len(source_info) - 1)
                end_line = start_line + 1

                line_type = source_info[start_line]
                while line_type is None:
                    start_line -= 1
                    line_type = source_info[start_line]

                while end_line < len(source_info) and source_info[end_line] is None:
                    end_line += 1

                end_line = min(end_line - 1, len(source_info))

                if line_type == 0:
                    verified = False
                    bp_msg = "Breakpoint cannot be set here."
                else:
                    verified = True
                    bp_msg = None

                bp = dap.Breakpoint(
                    id=bp_id,
                    verified=verified,
                    message=bp_msg,
                    source=msg.source,
                    line=start_line,
                    end_line=end_line,
                )

            self._breakpoints[bp_id] = AnsibleLineBreakpoint(
                id=bp_id,
                source=msg.source,
                source_breakpoint=source_breakpoint,
                breakpoint=bp,
                actual_path=actual_path,
                source_path=source_path,
            )
            breakpoint_info.append(bp)

        resp = dap.SetBreakpointsResponse(
            request_seq=msg.seq,
            breakpoints=breakpoint_info,
        )
        self.send(resp)

    @process_message.register
    def _(
        self,
        msg: dap.SetVariableRequest,
    ) -> None:
        strategy = self._get_strategy()
        resp = strategy.set_variable(msg)
        self.send(resp)

    @process_message.register
    def _(
        self,
        msg: dap.StackTraceRequest,
    ) -> None:
        strategy = self._get_strategy()
        resp = strategy.get_stacktrace(msg)
        self.send(resp)

    @process_message.register
    def _(
        self,
        msg: dap.StepInRequest,
    ) -> None:
        strategy = self._get_strategy()
        strategy.step_in(msg)
        self.send(dap.StepInResponse(request_seq=msg.seq))

    @process_message.register
    def _(
        self,
        msg: dap.StepOutRequest,
    ) -> None:
        strategy = self._get_strategy()
        strategy.step_out(msg)
        self.send(dap.StepOutResponse(request_seq=msg.seq))

    @process_message.register
    def _(
        self,
        msg: dap.ThreadsRequest,
    ) -> None:
        strategy = self._get_strategy()
        resp = strategy.get_threads(msg)
        self.send(resp)

    @process_message.register
    def _(
        self,
        msg: dap.VariablesRequest,
    ) -> None:
        strategy = self._get_strategy()
        resp = strategy.get_variables(msg)
        self.send(resp)

    @process_message.register
    def _(
        self,
        msg: dap.OutputEvent,
    ) -> None:
        # This should always be this category as only the da server sends this
        # message to the debuggee.
        if msg.category != DebugConfiguration.OUTPUT_CATEGORY or not isinstance(
            msg.data, DebugConfiguration
        ):  # pragma: nocover
            return

        self._debug_config = msg.data
