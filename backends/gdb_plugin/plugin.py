import logging
import gdb

import interfaces
from core import wl, output, PersistentUIState
from core.util import time_now
from . import libwayland_symbols
from . import extract

logger = logging.getLogger(__name__)

class Stream(output.stream.Base):
    def __init__(self, stream):
        self.stream = stream
    def override_write(self, string):
        gdb.write(string + '\n', self.stream)

def output_streams():
    # Both are stderr, because stdout does the annoying "enter to continue" thing
    return (Stream(gdb.STDERR), Stream(gdb.STDERR))

class WlConnectionDestroyBreakpoint(gdb.Breakpoint):
    def __init__(self, plugin):
        # Unclear what qualified=True means, but it doesn't break anything and improves total performance by ~5%
        super().__init__('wl_connection_destroy', internal=True, qualified=True)
        self.plugin = plugin
    def stop(self):
        connection = gdb.selected_frame().read_var('connection')
        connection_id = extract.connection_id_of(connection)
        self.plugin.close_connection(connection_id)
        return False

# This works, but breakpoints are expensive and we can create the connection when the first message comes in
'''
class WlConnectionCreateBreakpoint(gdb.Breakpoint):
    def __init__(self, plugin):
        # Unclear what qualified=True means, but it doesn't break anything and improves total performance by ~5%
        super().__init__('wl_connection_create', internal=True, qualified=True)
        self.plugin = plugin
    def stop(self):
        self.FinishBreakpoint(self.plugin)
        return False

    class FinishBreakpoint(gdb.FinishBreakpoint):
        def __init__(self, plugin):
            super().__init__(gdb.selected_frame(), internal=True)
            self.plugin = plugin
        def stop(self):
            connection_id = extract.connection_id_of(self.return_value)
            calling_function = str(gdb.selected_frame().function())
            if calling_function == 'wl_display_connect_to_fd':
                is_server = False
            elif calling_function == 'wl_client_create':
                is_server = True
            else:
                logger.warning(
                    'Unexpected function ' + calling_function + '() called wl_connection_create(), ' +
                    'unable to determine if connection is a client or server at this time')
                is_server = None
            self.plugin.open_connection(connection_id, is_server)
            return False
'''

class WlClosureCallBreakpoint(gdb.Breakpoint):
    def __init__(self, plugin, name, message_extractor):
        # Unclear what qualified=True means, but it doesn't break anything and improves total performance by ~5%
        super().__init__(name, internal=True, qualified=True)
        self.plugin = plugin
        self.message_extractor = message_extractor
    def stop(self):
        connection_id, message = self.message_extractor()
        self.plugin.process_message(connection_id, message)
        return self.plugin.paused()

class WlCommand(gdb.Command):
    'Issue a subcommand to Wayland Debug, use \'wl help\' for details'
    def __init__(self, plugin, prefix):
        super().__init__(prefix, gdb.COMMAND_DATA)
        self.plugin = plugin
    def invoke(self, arg, from_tty):
        self.plugin.invoke_command(arg)
    def complete(text, word):
        return None

class WlSubcommand(gdb.Command):
    'A Wayland debug command, use \'wl help\' for detail'
    def __init__(self, plugin, command):
        super().__init__('wl' + command, gdb.COMMAND_DATA)
        self.plugin = plugin
        self.command = command
    def invoke(self, arg, from_tty):
        self.plugin.invoke_command(self.command + ' ' + arg)
    def complete(text, word):
        return None

class Plugin:
    '''A GDB plugin (should only be instantiated when inside GDB)'''
    def __init__(self, out, connection_id_sink, command_sink, ui_state):
        assert isinstance(out, output.Output)
        assert isinstance(connection_id_sink, interfaces.ConnectionIDSink)
        assert isinstance(command_sink, interfaces.CommandSink)
        assert isinstance(ui_state, interfaces.UIState)
        self.out = out
        self.connection_id_sink = connection_id_sink
        self.command_sink = command_sink
        self.state = PersistentUIState(ui_state)
        # maps connection ids to thread numbers
        self.connection_threads = {}
        # Show full error messages in the case of a crash
        gdb.execute('set python print-stack full')
        if not self.out.show_unprocessed:
            # Suppress GDB output
            gdb.execute('set inferior-tty /dev/null')
        try:
            # GDB will automatically load the symbols when needed, but if we do it first we get to detect problems
            libwayland_symbols.verify()
        except RuntimeError as e:
            self.out.warn('Loading libwayland symbols failed: ' + str(e))
            self.out.warn('libwayland debug symbols were not found, so Wayland messages may not be detected in GDB mode')
            self.out.warn('See https://github.com/wmww/wayland-debug/blob/master/libwayland_debug_symbols.md for more information')
        #WlConnectionCreateBreakpoint(self)
        WlConnectionDestroyBreakpoint(self)
        WlClosureCallBreakpoint(self, 'wl_closure_invoke', extract.received_message)
        WlClosureCallBreakpoint(self, 'wl_closure_dispatch', extract.received_message)
        WlClosureCallBreakpoint(self, 'serialize_closure', extract.sent_message)
        WlCommand(self, 'w')
        WlCommand(self, 'wl')
        WlCommand(self, 'wayland')
        for command in command_sink.toplevel_commands():
            WlSubcommand(self, command)
        logger.info('Breakpoints: ' + repr(gdb.breakpoints()))

    def open_connection(self, connection_id, is_server):
        self.connection_threads[connection_id] = gdb.selected_thread().global_num
        self.connection_id_sink.open_connection(time_now(), connection_id, is_server)

    def close_connection(self, connection_id):
        del self.connection_threads[connection_id]
        self.connection_id_sink.close_connection(time_now(), connection_id)

    def process_message(self, connection_id, message):
        if self.state.paused():
            self.state.resume_requested()
        current_thread_num = gdb.selected_thread().global_num
        connection_thread_num = self.connection_threads.get(connection_id)
        if connection_thread_num is None:
            is_server = None
            if message.name == 'get_registry':
                is_server = not message.sent
            self.open_connection(connection_id, is_server)
        elif connection_thread_num != current_thread_num:
            self.out.warn(
                'Got message ' + str(message) +
                ' on thread ' + str(current_thread_num) +
                ' instead of connection\'s main thread ' + str(connection_thread_num))
        self.connection_id_sink.message(connection_id, message)

    def invoke_command(self, command):
        self.state.pause_requested()
        self.command_sink.process_command(command)
        if self.state.should_quit():
            gdb.execute('quit')
        elif not self.state.paused():
            gdb.execute('continue')

    def paused(self):
        return self.state.paused()
